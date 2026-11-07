from pathlib import Path
from types import SimpleNamespace

import pytest

from mabel.context.discovery import MabelContextDiscovery
from mabel.context.execution import BoundedCapabilityRunner
from mabel.repository import LocalRepository
from mabel.services.capabilities import CapabilityArgumentsError, CapabilityService
from mabel.services.capability_controls import CapabilityControlService
from mabel.state.capability_controls import SqliteCapabilityControlStore
from mabel.state.database import SqliteDatabase
from mabel.state.workflow_executions import SqliteWorkflowExecutionStore
from mabel.workflows.capability import WorkflowExecutionCapability
from mabel.workflows.execution import WorkflowExecutionService
from mabel.workflows.models import WorkflowActionResult


BASE = "a" * 40
COMMITTED = "b" * 40


class Projections:
    def __init__(self) -> None:
        root = Path(__file__).resolve().parents[2] / "mabel-context"
        self.discovery = MabelContextDiscovery(LocalRepository(root))
        self.context = self.discovery.load()
        self.commit_sha = BASE

    async def synchronize(self, owner_id: str):
        assert owner_id == "owner"
        return SimpleNamespace(
            discovery=self.discovery,
            context=self.context,
            commit_sha=self.commit_sha,
            root=Path(__file__).resolve().parents[2] / "mabel-context",
        )


class Handler:
    def __init__(self, action, callback):
        self.action = action
        self.callback = callback

    async def execute(self, invocation):
        return self.callback(invocation)


def _services(tmp_path: Path):
    projections = Projections()
    seen: dict[str, object] = {}

    def prepare(invocation):
        seen["arguments"] = invocation.execution.invocation_arguments
        return WorkflowActionResult("success", {"specification": {"id": "example"}})

    def development(_):
        return WorkflowActionResult("success", {"changes": ["capabilities/example"], "validation": {"tests": "passed"}})

    def gate(invocation):
        return WorkflowActionResult("stopped", {"gate_id": invocation.step.parameters["gate_id"]})

    def draft(_):
        return WorkflowActionResult(
            "success",
            {"draft": {"draft_id": "draft-1", "status": "pending", "files": [
                {"path": "/capabilities/example/executor.py", "diff": "def example(): ..."},
            ]}},
            proposal_id="draft-1",
        )

    def commit(invocation):
        assert invocation.execution.proposal_id == "draft-1"
        projections.commit_sha = COMMITTED
        return WorkflowActionResult("success", {"draft_id": "draft-1"}, committed_revision=COMMITTED)

    handlers = {
        "prepare_capability_specification": Handler("prepare", prepare),
        "invoke_governed_agent": Handler("development", development),
        "evaluate_development_evidence": Handler("evaluate", lambda _: WorkflowActionResult(
            "success", {"development": {"validation": {"tests": "passed"}, "changes": {"changed_files": ["capabilities/example/executor.py"]}}}
        )),
        "operator_gate": Handler("gate", gate),
        "verify_capability_proposal": Handler("verify", lambda _: WorkflowActionResult("success", {"verified": True})),
        "create_repository_draft": Handler("draft", draft),
        "commit_approved_draft": Handler("commit", commit),
        "post_commit_capability_proof": Handler("proof", lambda _: WorkflowActionResult(
            "success", {"capability_id": "example", "repository_commit": COMMITTED, "result": {"value": "123456"}}
        )),
    }
    database = SqliteDatabase(tmp_path / "state.db")
    database.initialize()
    execution = WorkflowExecutionService(projections, SqliteWorkflowExecutionStore(database), handlers)
    controls = CapabilityControlService(projections, SqliteCapabilityControlStore(database))
    capabilities = CapabilityService(
        projections, BoundedCapabilityRunner(), controls=controls,
        workflow_execution=WorkflowExecutionCapability(execution),
    )
    return capabilities, seen


def _start(arguments: object = None) -> dict[str, object]:
    return {
        "operation": "start",
        "workflow_path": "/workflows/capabilities/create-capability.md",
        "arguments": {"intent": "Create the 123456 capability"} if arguments is None else arguments,
    }


async def test_run_workflow_is_discovered_and_persists_governed_start_resume(tmp_path: Path) -> None:
    capabilities, seen = _services(tmp_path)

    index = await capabilities.get_capability_index("owner")
    assert "run_workflow" in {item["id"] for item in index["capabilities"]}
    assert (await capabilities.run_capability("owner", "echo")).result == {"message": "hello world"}

    first = await capabilities.run_capability("owner", "run_workflow", _start())
    assert first.repository_commit == BASE
    assert first.result["base_revision"] == BASE
    assert first.result["status"] == "stopped"
    assert first.result["active_gate"] == {"id": "development_result_review"}
    assert seen["arguments"] == {"intent": "Create the 123456 capability"}

    with pytest.raises(CapabilityArgumentsError):
        await capabilities.run_capability("owner", "run_workflow", {
            "operation": "resume", "execution_id": first.result["execution_id"],
            "gate_id": "exact_diff_approval", "decision": "approved",
        })

    second = await capabilities.run_capability("owner", "run_workflow", {
        "operation": "resume", "execution_id": first.result["execution_id"],
        "gate_id": "development_result_review", "decision": "approved",
    })
    assert second.result["execution_id"] == first.result["execution_id"]
    assert second.result["active_gate"] == {"id": "exact_diff_approval"}
    assert second.result["proposal_id"] == "draft-1"
    assert second.result["review"]["draft"]["files"] == [
        {"path": "/capabilities/example/executor.py", "diff": "def example(): ..."},
    ]

    completed = await capabilities.run_capability("owner", "run_workflow", {
        "operation": "resume", "execution_id": first.result["execution_id"],
        "gate_id": "exact_diff_approval", "decision": "approved",
    })
    assert completed.result["status"] == "completed"
    assert completed.result["committed_revision"] == COMMITTED
    assert completed.result["proof"] == {
        "capability_id": "example", "repository_commit": COMMITTED, "result": {"value": "123456"},
    }


async def test_declining_exact_diff_never_dispatches_commit(tmp_path: Path) -> None:
    capabilities, _ = _services(tmp_path)
    first = await capabilities.run_capability("owner", "run_workflow", _start())
    second = await capabilities.run_capability("owner", "run_workflow", {
        "operation": "resume", "execution_id": first.result["execution_id"],
        "gate_id": "development_result_review", "decision": "approved",
    })

    declined = await capabilities.run_capability("owner", "run_workflow", {
        "operation": "resume", "execution_id": second.result["execution_id"],
        "gate_id": "exact_diff_approval", "decision": "declined",
    })

    assert declined.result["status"] == "stopped"
    assert declined.result["committed_revision"] is None


@pytest.mark.parametrize("payload", [
    {"operation": "unknown"},
    {"operation": "start", "workflow_path": "/workflows/capabilities/create-capability.md"},
    _start('{"intent":"stringified"}'),
    {"operation": "resume", "gate_id": "development_result_review", "decision": "approved"},
    {**_start(), "provider": "caller-controlled"},
    {**_start(), "model": "caller-controlled"},
    {**_start(), "workspace_root": "/tmp"},
])
async def test_run_workflow_rejects_non_governed_request_shapes(tmp_path: Path, payload: dict[str, object]) -> None:
    capabilities, _ = _services(tmp_path)

    with pytest.raises(CapabilityArgumentsError):
        await capabilities.run_capability("owner", "run_workflow", payload)
