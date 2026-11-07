from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from mabel.services import development_agents
from mabel.repositories.credentials import GitHubTokenCredential
from mabel.repositories.models import ManagedRepository
from mabel.services.development_agents import (
    CodexCliDevelopmentProvider,
    DevelopmentAgentError,
    DevelopmentAgentRequest,
    DevelopmentAgentService,
    DevelopmentProviderResult,
)
from mabel.services.development_workspace import (
    DevelopmentWorkspaceError,
    DevelopmentWorkspaceService,
    GitHubDevelopmentCheckout,
)


class Repositories:
    def __init__(self, repository: ManagedRepository) -> None:
        self.repository = repository

    def resolve_active_context_repository(self, owner_id: str) -> ManagedRepository:
        assert owner_id == self.repository.owner_id
        return self.repository


class Credentials:
    async def resolve(self, reference: str) -> object:
        return GitHubTokenCredential("test-token")


@dataclass
class Provider:
    result: DevelopmentProviderResult
    write: tuple[str, str] | None = None
    calls: list[DevelopmentAgentRequest] | None = None

    async def execute(self, request: DevelopmentAgentRequest) -> DevelopmentProviderResult:
        if self.calls is not None:
            self.calls.append(request)
        if self.write is not None:
            path, content = self.write
            target = request.workspace.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return self.result


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, check=True,
    )
    return completed.stdout.strip()


def _source_repository(tmp_path: Path) -> tuple[Path, str, str]:
    root = tmp_path / "source"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "tests@example.test")
    _git(root, "config", "user.name", "MABEL tests")
    (root / "capabilities").mkdir()
    (root / "capabilities" / "manifest.json").write_text("{}\n", encoding="utf-8")
    (root / "unrelated.txt").write_text("original\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    (root / "capabilities" / "after.txt").write_text("later\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "later")
    return root, base, _git(root, "rev-parse", "HEAD")


def _service(tmp_path: Path, source: Path, *, projection: Path | None = None) -> DevelopmentWorkspaceService:
    now = datetime.now(timezone.utc)
    repository = ManagedRepository(
        id=uuid4(), owner_id="owner", provider="github", purpose="mabel_context_repository",
        display_name="Context", provider_repository_id="1", canonical_locator="ignored", default_branch="main",
        credential_id=uuid4(), status="connected", enabled=True, created_at=now, updated_at=now,
    )
    return DevelopmentWorkspaceService(
        Repositories(repository), Credentials(), GitHubDevelopmentCheckout(lambda _: str(source)),
        tmp_path / "workspaces", projection_root=projection,
    )


@pytest.mark.asyncio
async def test_workspace_checks_out_the_exact_requested_revision_and_is_writable(tmp_path: Path) -> None:
    source, base, newest = _source_repository(tmp_path)
    service = _service(tmp_path, source)

    workspace = await service.create("owner", base, ["capabilities"])

    assert _git(workspace.root, "rev-parse", "HEAD") == base
    assert base != newest
    (workspace.root / "capabilities" / "new.py").write_text("value = 1\n", encoding="utf-8")
    changes = service.collect_changes(workspace)
    assert [str(path) for path in changes.changed_files] == ["capabilities/new.py"]
    assert "capabilities/new.py" in changes.patch
    assert changes.proposed_files == ({"path": "capabilities/new.py", "change": "replace", "content": "value = 1\n"},)


@pytest.mark.asyncio
async def test_workspace_rejects_unknown_revision_and_never_reuses_projection(tmp_path: Path) -> None:
    source, _, _ = _source_repository(tmp_path)
    projection = tmp_path / "projection"
    projection.mkdir()
    service = _service(tmp_path, source, projection=projection)

    with pytest.raises(DevelopmentWorkspaceError, match="base_revision_unavailable"):
        await service.create("owner", "a" * 40, ["capabilities"])

    workspace = await service.create("owner", _git(source, "rev-parse", "HEAD"), ["capabilities"])
    assert workspace.root != projection
    with pytest.raises(ValueError, match="must not share"):
        _service(tmp_path, source, projection=tmp_path / "workspaces")


@pytest.mark.asyncio
async def test_workspace_independently_rejects_unauthorized_changes_and_cleans_up(tmp_path: Path) -> None:
    source, base, _ = _source_repository(tmp_path)
    service = _service(tmp_path, source)
    workspace = await service.create("owner", base, ["capabilities"])
    (workspace.root / "unrelated.txt").write_text("bad\n", encoding="utf-8")

    with pytest.raises(DevelopmentWorkspaceError, match="unauthorized_workspace_change"):
        service.collect_changes(workspace)

    service.cleanup(workspace)
    service.cleanup(workspace)
    assert not workspace.root.exists()


@pytest.mark.asyncio
async def test_workspaces_are_isolated(tmp_path: Path) -> None:
    source, base, _ = _source_repository(tmp_path)
    service = _service(tmp_path, source)
    first = await service.create("owner", base, ["capabilities"])
    second = await service.create("owner", base, ["capabilities"])
    (first.root / "capabilities" / "one.py").write_text("one\n", encoding="utf-8")

    assert first.root != second.root
    assert not (second.root / "capabilities" / "one.py").exists()


@pytest.mark.asyncio
async def test_agent_dispatches_only_code_development_and_collects_workspace_changes(tmp_path: Path) -> None:
    source, base, _ = _source_repository(tmp_path)
    workspaces = _service(tmp_path, source)
    workspace = await workspaces.create("owner", base, ["capabilities"])
    calls: list[DevelopmentAgentRequest] = []
    provider = Provider(
        DevelopmentProviderResult("success", {"tests": ["pytest"], "token": "never-return"}),
        write=("capabilities/new.py", "value = 1\n"), calls=calls,
    )
    agents = DevelopmentAgentService(provider, workspaces)
    request = DevelopmentAgentRequest("code_development", workspace, base, {"id": "example"}, {"result": {}}, "execution")

    result = await agents.run(request)

    assert calls == [request]
    assert result.status == "success"
    assert result.evidence["changes"]["changed_files"] == ["capabilities/new.py"]
    assert result.evidence["validation"]["token"] == "[redacted]"
    assert _git(workspace.root, "rev-parse", "HEAD") == base
    with pytest.raises(DevelopmentAgentError, match="unknown_agent_class"):
        await agents.run(DevelopmentAgentRequest("anything_else", workspace, base, {}, {}, "execution"))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["blocked", "failed", "resource_limited"])
async def test_agent_preserves_provider_structured_status(tmp_path: Path, status: str) -> None:
    source, base, _ = _source_repository(tmp_path)
    workspaces = _service(tmp_path, source)
    workspace = await workspaces.create("owner", base, ["capabilities"])
    agents = DevelopmentAgentService(Provider(DevelopmentProviderResult(status)), workspaces)

    result = await agents.run(DevelopmentAgentRequest("code_development", workspace, base, {}, {}, "execution"))

    assert result.status == status


@pytest.mark.asyncio
async def test_agent_rejects_provider_changes_outside_the_workspace_scope(tmp_path: Path) -> None:
    source, base, _ = _source_repository(tmp_path)
    workspaces = _service(tmp_path, source)
    workspace = await workspaces.create("owner", base, ["capabilities"])
    agents = DevelopmentAgentService(
        Provider(DevelopmentProviderResult("success"), write=("unrelated.txt", "nope\n")), workspaces
    )

    with pytest.raises(DevelopmentAgentError, match="unauthorized_workspace_change"):
        await agents.run(DevelopmentAgentRequest("code_development", workspace, base, {}, {}, "execution"))


@pytest.mark.asyncio
async def test_terminal_provider_failure_keeps_redacted_diagnostics_and_skips_change_collection(tmp_path: Path) -> None:
    source, base, _ = _source_repository(tmp_path)
    workspaces = _service(tmp_path, source)
    workspace = await workspaces.create("owner", base, ["capabilities"])
    agents = DevelopmentAgentService(
        Provider(
            DevelopmentProviderResult("failed", {"final_message": "token=live-secret " + "x" * 5000}),
            write=("unrelated.txt", "must not be collected\\n"),
        ),
        workspaces,
    )

    result = await agents.run(DevelopmentAgentRequest("code_development", workspace, base, {}, {}, "execution"))

    assert result.status == "failed"
    assert result.evidence["changes"] == {"changed_files": [], "patch": "", "proposed_files": []}
    final_message = result.evidence["validation"]["final_message"]
    assert final_message.startswith("token=[redacted]")
    assert len(final_message) == 4096


@pytest.mark.asyncio
async def test_codex_binding_captures_final_message_and_uses_supported_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, base, _ = _source_repository(tmp_path)
    workspaces = _service(tmp_path, source)
    workspace = await workspaces.create("owner", base, ["capabilities"])
    request = DevelopmentAgentRequest("code_development", workspace, base, {}, {}, "execution")
    captured: dict[str, object] = {}
    statuses = iter((7, 0))

    def run_bounded(arguments, prompt, root, timeout, maximum, codex_home):
        captured["arguments"] = arguments
        final_message = Path(arguments[arguments.index("--output-last-message") + 1])
        final_message.write_text("diagnostic " + "x" * 5000, encoding="utf-8")
        return next(statuses), 12

    monkeypatch.setattr(development_agents, "_run_bounded", run_bounded)
    failed = await CodexCliDevelopmentProvider("codex").execute(request)

    arguments = captured["arguments"]
    assert "--ask-for-approval" not in arguments
    assert "--output-last-message" in arguments
    assert failed.status == "failed"
    assert failed.validation is not None
    assert len(failed.validation["final_message"]) == 4096
    assert failed.validation["exit_code"] == 7

    succeeded = await CodexCliDevelopmentProvider("codex").execute(request)

    assert succeeded.status == "success"
    assert succeeded.validation is not None
    assert len(succeeded.validation["final_message"]) == 4096


def test_bounded_runner_preserves_the_service_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, _, _ = _source_repository(tmp_path)
    captured: dict[str, object] = {}

    def capture_environment(*args, **kwargs):
        captured.update(kwargs["env"])
        raise OSError("test subprocess")

    monkeypatch.setenv("PATH", "/srv/mabel/bin")
    monkeypatch.setattr(development_agents.subprocess, "Popen", capture_environment)

    with pytest.raises(OSError, match="test subprocess"):
        development_agents._run_bounded(["codex"], b"", source, 1, 4096, None)

    assert captured["PATH"] == "/srv/mabel/bin"


@pytest.mark.asyncio
async def test_codex_binding_maps_bounded_timeout_and_failure_without_returning_output(tmp_path: Path) -> None:
    source, base, _ = _source_repository(tmp_path)
    workspaces = _service(tmp_path, source)
    workspace = await workspaces.create("owner", base, ["capabilities"])
    request = DevelopmentAgentRequest("code_development", workspace, base, {}, {}, "execution")
    sleeping = tmp_path / "sleeping-provider"
    sleeping.write_text("#!" + sys.executable + "\nimport time\ntime.sleep(1)\n", encoding="utf-8")
    sleeping.chmod(0o700)
    provider = CodexCliDevelopmentProvider(str(sleeping))
    provider._TIMEOUT_SECONDS = 0.05

    timed_out = await provider.execute(request)

    assert timed_out.status == "resource_limited"
    failing = tmp_path / "failing-provider"
    failing.write_text("#!" + sys.executable + "\nraise SystemExit(7)\n", encoding="utf-8")
    failing.chmod(0o700)
    failed = await CodexCliDevelopmentProvider(str(failing)).execute(request)
    assert failed.status == "failed"
