import subprocess
import os
import json
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from mabel.app import create_app
from mabel.config import Settings
from mabel.context.execution import BoundedCapabilityRunner
from mabel.context.projection import ContextProjectionService, GitHubGitCheckout
from mabel.repositories.credentials import ManagedRepositoryCredentialStore
from mabel.repositories.models import RepositoryVerification
from mabel.services.capabilities import CapabilityService
from mabel.services.capability_controls import CapabilityControlService
from mabel.services.managed_repositories import ManagedRepositoryService
from mabel.services.workspace import ContextWorkspaceService
from mabel.state.database import SqliteDatabase
from mabel.state.capability_controls import SqliteCapabilityControlStore
from mabel.state.managed_repositories import SqliteManagedRepositoryStore
from mabel.state.projections import SqliteRepositoryProjectionStore


@dataclass
class FakeGitHub:
    verification: RepositoryVerification = RepositoryVerification("123", "owner/mabel-context", "main")

    async def verify(self, locator, credential):
        return self.verification


def _write_context(path: Path, executor: str | None = None) -> None:
    files = {
        "manifest.json": '''{"schema_version": 1, "system": {"role": "user_operational_context"}, "instructions": {"start": "instructions/start.md"}, "schemas": {"manifest": "schemas/manifest.json"}, "capabilities": {"manifest": "capabilities/manifest.json"}}''',
        "instructions/start.md": "# Context instructions\n",
        "schemas/manifest.json": '''{"schema_version": 1, "content_type": "manifest", "schemas": [{"id": "capability", "title": "Capability Schema", "content_type": "schema", "path": "schemas/capability.schema.json"}], "rules": [{"id": "capability", "title": "Capability Rules", "content_type": "rules", "path": "schemas/capability.rules.md"}]}''',
        "schemas/capability.schema.json": "{}\n",
        "schemas/capability.rules.md": "# Capability rules\n",
        "capabilities/manifest.json": '''{"schema_version": 1, "capabilities": [{"id": "echo", "title": "Echo", "content_type": "capability", "path": "capabilities/echo/capability.json", "version": "1.0"}]}''',
        "capabilities/echo/capability.json": '''{"schema_version": 1, "id": "echo", "executor": {"runtime": "python_callable_v1", "path": "capabilities/echo/executor.py", "callable": "echo"}, "tests_path": "capabilities/echo/tests", "contract": {"input": "none", "output": "json_object"}}''',
        "capabilities/echo/executor.py": executor or 'def echo():\n    return {"message": "hello world"}\n',
        "capabilities/echo/tests/test_echo.py": "def test_echo():\n    assert True\n",
        "unrelated/private.txt": "must never be visible\n",
    }
    for relative_path, content in files.items():
        target = path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _git(arguments: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=cwd, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


def _tree_node(nodes: list[dict[str, object]], path: str) -> dict[str, object]:
    for node in nodes:
        if node["path"] == path:
            return node
        children = node.get("children")
        if isinstance(children, list):
            try:
                return _tree_node(children, path)
            except KeyError:
                pass
    raise KeyError(path)


async def _workspace_client(
    tmp_path: Path, executor: str | None = None, unsafe_test_symlink: bool = False
):
    source = tmp_path / "source"
    source.mkdir()
    _write_context(source, executor)
    if unsafe_test_symlink:
        outside = tmp_path / "outside.py"
        outside.write_text("secret = 'outside projection'\n", encoding="utf-8")
        os.symlink(outside, source / "capabilities/echo/tests/escape.py")
    _git(["init", "-b", "main"], source)
    _git(["config", "user.email", "test@example.invalid"], source)
    _git(["config", "user.name", "Test"], source)
    _git(["add", "."], source)
    _git(["commit", "-m", "context"], source)
    commit = _git(["rev-parse", "HEAD"], source)
    remote = tmp_path / "remote.git"
    _git(["init", "--bare", str(remote)], tmp_path)
    _git(["remote", "add", "origin", str(remote)], source)
    _git(["push", "origin", "main"], source)

    database_path = tmp_path / "state.db"
    database = SqliteDatabase(database_path)
    database.initialize()
    credentials = ManagedRepositoryCredentialStore(database, Fernet.generate_key().decode("utf-8"))
    repositories = ManagedRepositoryService(SqliteManagedRepositoryStore(database), credentials, FakeGitHub())
    await repositories.register(
        owner_id="test-owner", display_name="Context", provider="github",
        purpose="mabel_context_repository", repository="owner/mabel-context", github_token="token",
    )
    projections = ContextProjectionService(
        repositories, credentials, GitHubGitCheckout(lambda _: str(remote)),
        tmp_path / "projections", SqliteRepositoryProjectionStore(database),
    )
    controls = CapabilityControlService(projections, SqliteCapabilityControlStore(database))
    capabilities = CapabilityService(projections, BoundedCapabilityRunner(), controls=controls)
    workspace = ContextWorkspaceService(projections, capabilities)
    app = create_app(
        managed_repository_service=repositories, capability_service=capabilities, workspace_service=workspace,
        settings=Settings(state_database_path=database_path, development_owner_id="test-owner"),
    )
    return app, commit, source


async def test_workspace_snapshot_tree_file_detail_and_run_are_commit_pinned(tmp_path: Path) -> None:
    app, commit, _ = await _workspace_client(tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            snapshot = await client.get("/api/portal/workspace")
            tree = await client.get(f"/api/portal/workspace/tree?commit={commit}")
            file = await client.get(f"/api/portal/workspace/file?commit={commit}&path=capabilities/echo/executor.py")
            detail = await client.get(f"/api/portal/workspace/capabilities/echo?commit={commit}")
            run = await client.post("/api/portal/workspace/capabilities/echo/run")

    assert snapshot.status_code == 200
    assert snapshot.json()["projection"] == {
        "status": "synchronized", "commit_sha": commit,
        "synced_at": snapshot.json()["projection"]["synced_at"], "read_only": True,
    }
    assert snapshot.json()["capabilities"] == [{
        "id": "echo", "title": "Echo", "path": "capabilities/echo/capability.json",
        "version": "1.0", "status": "available",
    }]
    assert tree.status_code == 200
    assert tree.json()["repository_commit"] == commit
    assert "unrelated" not in str(tree.json()["tree"])
    schema = _tree_node(tree.json()["tree"], "schemas/capability.schema.json")
    assert schema == {
        "name": "capability.schema.json",
        "path": "schemas/capability.schema.json",
        "type": "file",
        "title": "Capability Schema",
        "content_type": "schema",
        "id": "capability",
    }
    capability = _tree_node(tree.json()["tree"], "capabilities/echo/capability.json")
    assert capability["name"] == "capability.json"
    assert capability["title"] == "Echo"
    assert capability["content_type"] == "capability"
    assert capability["id"] == "echo"
    capability_root = _tree_node(tree.json()["tree"], "capabilities/echo")
    assert capability_root["name"] == "echo"
    assert "title" not in capability_root
    assert [node["path"] for node in _tree_node(tree.json()["tree"], "capabilities")["children"]] == [
        "capabilities/echo", "capabilities/manifest.json"
    ]
    assert [node["path"] for node in capability_root["children"]] == [
        "capabilities/echo/tests", "capabilities/echo/capability.json", "capabilities/echo/executor.py"
    ]
    assert file.json() == {
        "repository_commit": commit, "path": "capabilities/echo/executor.py",
        "content": 'def echo():\n    return {"message": "hello world"}\n',
        "language": "python", "read_only": True,
    }
    assert detail.json() == {
        "repository_commit": commit, "id": "echo", "title": "Echo", "version": "1.0",
        "status": "available", "runtime": "python_callable_v1",
        "executor": {"path": "capabilities/echo/executor.py", "callable": "echo"},
        "tests": {"path": "capabilities/echo/tests", "declared": True},
    }
    assert run.json()["repository_commit"] == commit
    assert run.json()["status"] == "succeeded"
    assert run.json()["result"] == {"message": "hello world"}
    assert json.loads(run.json()["stdout"]) == {"message": "hello world"}
    assert run.json()["stderr"] == ""


async def test_workspace_rejects_stale_commits_unsafe_and_undeclared_paths(tmp_path: Path) -> None:
    app, commit, _ = await _workspace_client(tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            stale = await client.get(f"/api/portal/workspace/tree?commit={'a' * 40}")
            undeclared = await client.get(f"/api/portal/workspace/file?commit={commit}&path=unrelated/private.txt")
            traversal = await client.get(f"/api/portal/workspace/file?commit={commit}&path=../manifest.json")

    assert stale.status_code == 409
    assert "stale" in stale.json()["detail"]
    assert undeclared.status_code == 409
    assert "not declared" in undeclared.json()["detail"]
    assert traversal.status_code == 409
    assert "repository-relative" in traversal.json()["detail"]


async def test_workspace_run_report_bounds_oversized_child_output(tmp_path: Path) -> None:
    app, _, _ = await _workspace_client(
        tmp_path, 'def echo():\n    print("x" * 70000)\n    return {"message": "hello world"}\n'
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            report = await client.post("/api/portal/workspace/capabilities/echo/run")

    assert report.status_code == 200
    assert report.json()["status"] == "failed"
    assert report.json()["result"] if "result" in report.json() else None is None
    assert len(report.json()["stdout"].encode("utf-8")) <= 64 * 1024
    assert report.json()["error"] == "Capability execution exceeded its output limit."


async def test_workspace_rejects_a_symlink_that_escapes_a_declared_test_directory(tmp_path: Path) -> None:
    app, commit, _ = await _workspace_client(tmp_path, unsafe_test_symlink=True)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            tree = await client.get(f"/api/portal/workspace/tree?commit={commit}")

    assert tree.status_code == 409
    assert "safely" in tree.json()["detail"]


async def test_workspace_rejects_an_old_real_commit_after_the_projection_advances(tmp_path: Path) -> None:
    app, first_commit, source = await _workspace_client(tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            first = await client.get("/api/portal/workspace")
            (source / "instructions/start.md").write_text("# Updated instructions\n", encoding="utf-8")
            _git(["add", "instructions/start.md"], source)
            _git(["commit", "-m", "update context"], source)
            _git(["push", "origin", "main"], source)
            refreshed = await client.post("/api/portal/workspace/refresh")
            stale = await client.get(
                f"/api/portal/workspace/file?commit={first_commit}&path=instructions/start.md"
            )

    assert first.status_code == 200
    assert first.json()["projection"]["commit_sha"] == first_commit
    assert refreshed.status_code == 200
    assert refreshed.json()["projection"]["commit_sha"] != first_commit
    assert stale.status_code == 409
    assert "stale" in stale.json()["detail"]


async def test_portal_exposes_workspace_shell_and_assets(tmp_path: Path) -> None:
    app = create_app(settings=Settings(state_database_path=tmp_path / "state.db"))
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            portal = await client.get("/portal")
            script = await client.get("/portal/static/workspace.js")
            stylesheet = await client.get("/portal/static/workspace.css")
            workspace = await client.get("/api/portal/workspace")

    assert portal.status_code == 200
    assert "MABEL workspace" in portal.text
    assert "Read-only" in portal.text
    assert "Declared repository files" in portal.text
    assert "/portal/static/workspace.js" in portal.text
    assert script.status_code == 200
    assert "api/portal/workspace" in script.text
    assert "textContent" in script.text
    assert 'createElement("details")' not in script.text
    assert "tree-directory" in script.text
    assert "treeMetadata" in script.text
    assert "revealTreePath" in script.text
    assert "expandDirectories" not in script.text
    assert stylesheet.status_code == 200
    assert ".workspace-layout" in stylesheet.text
    assert workspace.status_code == 409
