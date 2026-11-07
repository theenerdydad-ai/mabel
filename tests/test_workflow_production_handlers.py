from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from mabel.context.discovery import MabelContextDiscovery
from mabel.repositories.credentials import GitHubTokenCredential
from mabel.repositories.models import ManagedRepository
from mabel.repository import LocalRepository
from mabel.services.development_agents import DevelopmentAgentService, DevelopmentProviderResult
from mabel.services.development_workspace import DevelopmentWorkspaceService, GitHubDevelopmentCheckout
from mabel.services.capabilities import CapabilityRunResult
from mabel.state.database import SqliteDatabase
from mabel.state.workflow_executions import SqliteWorkflowExecutionStore
from mabel.workflows.execution import WorkflowExecutionService
from mabel.workflows.capability import WorkflowExecutionCapability
from mabel.workflows.production import ProductionWorkflowHandlers


BASE = "1" * 40
COMMITTED = "2" * 40


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()


class Repositories:
    def __init__(self, repository): self.repository = repository
    def resolve_active_context_repository(self, owner_id): return self.repository


class Credentials:
    async def resolve(self, reference): return GitHubTokenCredential("test-token")


class Provider:
    async def execute(self, request):
        root = request.workspace.root
        specification = request.brief["specification"]
        capability_id = specification["capability_id"]
        (root / "capabilities" / capability_id / "tests").mkdir(parents=True, exist_ok=True)
        (root / specification["capability_path"]).write_text(json.dumps({
            "content_type": "mabel_capability", "schema_version": "1.0", "id": capability_id,
            "title": specification["title"], "description": specification["description"], "version": "1.0",
            "runtime_binding": specification["runtime_binding"], "input_contract": specification["input_contract"],
            "output_contract": specification["output_contract"], "effects": specification["effects"],
            "verification": {"tests_path": "/" + specification["tests_path"], "execution_proof": "required"},
        }), encoding="utf-8")
        (root / specification["executor_path"]).write_text("def example():\n    return {'value': '123456'}\n", encoding="utf-8")
        (root / specification["tests_path"] / "test_example.py").write_text("def test_example():\n    assert True\n", encoding="utf-8")
        (root / specification["catalog_path"]).write_text(json.dumps({"content_type": "mabel_capability_catalog", "capabilities": [{
            "id": capability_id, "title": specification["title"], "path": "/" + specification["capability_path"],
            "version": "1.0", "schema_version": "1.0",
        }]}), encoding="utf-8")
        return DevelopmentProviderResult("success", {"tests": ["tests/test_example.py"]})


class OutOfScopeProvider:
    async def execute(self, request):
        (request.workspace.root / "unrelated.txt").write_text("not allowed\\n", encoding="utf-8")
        return DevelopmentProviderResult("success", {"provider": "test"})


class Amie:
    def __init__(self, projections): self.calls = []; self.projections = projections
    async def run(self, owner_id, operation, arguments):
        self.calls.append((operation, dict(arguments)))
        if operation == "create_draft":
            return {"draft_id": "draft-1", "status": "pending", "files": [{"path": item["path"], "diff": "diff"} for item in arguments["files"]]}
        if operation == "get_draft": return {"draft_id": "draft-1", "status": "pending", "files": []}
        if operation == "commit_draft":
            self.projections.revision = COMMITTED
            return {"draft_id": "draft-1", "status": "committed", "github_commit_sha": COMMITTED}
        raise AssertionError(operation)


class Capabilities:
    def __init__(self): self.calls = []
    async def get_capability_index(self, owner_id):
        self.calls.append("index")
        return {"repository_commit": COMMITTED, "capabilities": [{"id": "example"}]}
    async def run_capability(self, owner_id, capability_id):
        self.calls.append(("run", capability_id))
        return CapabilityRunResult(capability_id, COMMITTED, {"value": "123456"})


class Projections:
    def __init__(self, definition, revision):
        self.definition, self.revision = definition, revision
    async def synchronize(self, owner_id):
        return SimpleNamespace(
            commit_sha=self.revision, discovery=SimpleNamespace(load_workflow=lambda workflow_id: self.definition),
            context=SimpleNamespace(governance_paths=()),
        )


def _workspace_service(tmp_path: Path) -> tuple[DevelopmentWorkspaceService, str]:
    source = tmp_path / "source"; source.mkdir()
    _git(source, "init", "-b", "main"); _git(source, "config", "user.email", "test@example.test"); _git(source, "config", "user.name", "Tests")
    (source / "capabilities").mkdir(); (source / "capabilities" / "capabilities-manifest.json").write_text("{}", encoding="utf-8")
    _git(source, "add", "."); _git(source, "commit", "-m", "base")
    base = _git(source, "rev-parse", "HEAD")
    now = datetime.now(timezone.utc)
    repository = ManagedRepository(uuid4(), "owner", "github", "mabel_context_repository", "Context", "1", "ignored", "main", uuid4(), "connected", True, now, now)
    return DevelopmentWorkspaceService(Repositories(repository), Credentials(), GitHubDevelopmentCheckout(lambda _: str(source)), tmp_path / "workspaces"), base


def _definition():
    root = Path("/Users/davidpalmisano/Documents/Codex/mabel-context")
    return MabelContextDiscovery(LocalRepository(root)).load_workflow("create_capability")


@pytest.mark.asyncio
async def test_production_handlers_walk_the_real_create_capability_plan(tmp_path: Path) -> None:
    workspaces, base = _workspace_service(tmp_path)
    definition = _definition()
    projections = Projections(definition, base)
    agents = DevelopmentAgentService(Provider(), workspaces)
    amie = Amie(projections); capabilities = Capabilities()
    handlers = ProductionWorkflowHandlers(projections, workspaces, agents, amie, capabilities).handlers()
    database = SqliteDatabase(tmp_path / "state.db"); database.initialize()
    service = WorkflowExecutionService(projections, SqliteWorkflowExecutionStore(database), handlers)
    arguments = {
        "capability_id": "example", "title": "Example", "description": "Return the governed example.",
        "input_contract": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "output_contract": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"], "additionalProperties": False},
        "effects": [], "expected_result": {"value": "123456"},
    }

    first = await service.start("owner", "create_capability", "workflows/capabilities/create-capability.md", arguments, base)
    assert first.active_gate_id == "development_result_review"
    assert not amie.calls
    second = await service.resume("owner", first.execution_id, "development_result_review", "approved")
    assert second.active_gate_id == "exact_diff_approval", second.step_results
    assert second.proposal_id == "draft-1"
    assert [operation for operation, _ in amie.calls] == ["create_draft"]
    completed = await service.resume("owner", second.execution_id, "exact_diff_approval", "approved")

    assert completed.status == "completed"
    assert completed.committed_revision == COMMITTED
    assert [operation for operation, _ in amie.calls] == ["create_draft", "get_draft", "commit_draft"]
    assert capabilities.calls == ["index", ("run", "example")]
    assert not list((tmp_path / "workspaces").rglob("executor.py"))


@pytest.mark.asyncio
async def test_workflow_capability_adapter_uses_production_handler_composition(tmp_path: Path) -> None:
    workspaces, base = _workspace_service(tmp_path)
    definition = _definition()
    projections = Projections(definition, base)
    runtime = WorkflowExecutionService(
        projections,
        SqliteWorkflowExecutionStore((database := SqliteDatabase(tmp_path / "state.db"))),
        ProductionWorkflowHandlers(
            projections, workspaces, DevelopmentAgentService(Provider(), workspaces), Amie(projections), Capabilities()
        ).handlers(),
    )
    database.initialize()
    adapter = WorkflowExecutionCapability(runtime)
    capability_projection = SimpleNamespace(
        commit_sha=base, context=SimpleNamespace(workflows=(definition.summary,)),
    )
    arguments = {
        "capability_id": "example", "title": "Example", "description": "Return the governed example.",
        "input_contract": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "output_contract": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"], "additionalProperties": False},
        "effects": [], "expected_result": {"value": "123456"},
    }

    first = await adapter.run("owner", {
        "operation": "start", "workflow_path": "/workflows/capabilities/create-capability.md", "arguments": arguments,
    }, capability_projection)
    second = await adapter.run("owner", {
        "operation": "resume", "execution_id": first["execution_id"],
        "gate_id": "development_result_review", "decision": "approved",
    }, capability_projection)
    completed = await adapter.run("owner", {
        "operation": "resume", "execution_id": first["execution_id"],
        "gate_id": "exact_diff_approval", "decision": "approved",
    }, capability_projection)

    assert first["active_gate"] == {"id": "development_result_review"}
    assert second["proposal_id"] == "draft-1"
    assert second["review"]["draft"]["files"]
    assert completed["committed_revision"] == COMMITTED
    assert completed["proof"]["result"] == {"value": "123456"}


@pytest.mark.asyncio
async def test_production_handlers_block_when_development_is_not_enabled(tmp_path: Path) -> None:
    workspaces, base = _workspace_service(tmp_path)
    definition = _definition(); projections = Projections(definition, base)
    handlers = ProductionWorkflowHandlers(projections, workspaces, None, Amie(projections), Capabilities()).handlers()
    database = SqliteDatabase(tmp_path / "state.db"); database.initialize()
    service = WorkflowExecutionService(projections, SqliteWorkflowExecutionStore(database), handlers)
    arguments = {"id": "example", "title": "Example", "description": "Example", "input_contract": {"type": "object"}, "output_contract": {"type": "object"}, "effects": [], "expected_result": {}}

    result = await service.start("owner", "create_capability", "workflows/capabilities/create-capability.md", arguments, base)

    assert result.status == "blocked"


@pytest.mark.asyncio
async def test_production_handler_preserves_development_workspace_failure_reason(tmp_path: Path) -> None:
    workspaces, base = _workspace_service(tmp_path)
    definition = _definition(); projections = Projections(definition, base)
    handlers = ProductionWorkflowHandlers(
        projections, workspaces, DevelopmentAgentService(OutOfScopeProvider(), workspaces), Amie(projections), Capabilities()
    ).handlers()
    database = SqliteDatabase(tmp_path / "state.db"); database.initialize()
    service = WorkflowExecutionService(projections, SqliteWorkflowExecutionStore(database), handlers)
    arguments = {
        "capability_id": "example", "title": "Example", "description": "Return the governed example.",
        "input_contract": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "output_contract": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"], "additionalProperties": False},
        "effects": [], "expected_result": {"value": "123456"},
    }

    result = await service.start("owner", "create_capability", "workflows/capabilities/create-capability.md", arguments, base)

    assert result.status == "failed"
    assert result.step_results["invoke_development_agent"] == {
        "outcome": "failed", "evidence": {"reason": "unauthorized_workspace_change"},
    }
