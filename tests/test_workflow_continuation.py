import asyncio
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from mabel.context.discovery import MabelContextDiscovery
from mabel.repository import LocalRepository
from mabel.state.database import SqliteDatabase
from mabel.state.workflow_executions import SqliteWorkflowExecutionStore
from mabel.workflows.capability import WorkflowCapabilityError, WorkflowExecutionCapability
from mabel.workflows.continuation import WorkflowContinuationService
from mabel.workflows.execution import WorkflowExecutionError, WorkflowExecutionService
from mabel.workflows.models import WorkflowActionResult, WorkflowExecution
from mabel.workflows.production import validate_create_capability_admission


BASE = "a" * 40


class Projections:
    def __init__(self) -> None:
        root = Path(__file__).resolve().parents[2] / "mabel-context"
        self.discovery = MabelContextDiscovery(LocalRepository(root))
        self.definition = self.discovery.load_workflow("create_capability")
        self.context = SimpleNamespace(workflows=(self.definition.summary,))

    async def synchronize(self, owner_id: str):
        assert owner_id == "owner"
        return SimpleNamespace(discovery=self.discovery, context=self.context, commit_sha=BASE)


class Handler:
    def __init__(self, outcome: str = "success") -> None:
        self.outcome = outcome

    async def execute(self, _):
        return WorkflowActionResult(self.outcome, {})


class SlowHandler:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    async def execute(self, _):
        self.started.set()
        await asyncio.to_thread(self.release.wait)
        return WorkflowActionResult("blocked", {"reason": "slow_action_finished"})


def _handlers(slow: SlowHandler) -> dict[str, object]:
    return {
        "prepare_capability_specification": slow,
        "invoke_governed_agent": Handler(),
        "evaluate_development_evidence": Handler(),
        "operator_gate": Handler("stopped"),
        "verify_capability_proposal": Handler(),
        "create_repository_draft": Handler(),
        "commit_approved_draft": Handler(),
        "post_commit_capability_proof": Handler(),
    }


def _service(tmp_path, *, validator=False):
    projections = Projections()
    store = SqliteWorkflowExecutionStore(database := SqliteDatabase(tmp_path / "state.db"))
    database.initialize()
    slow = SlowHandler()
    execution = WorkflowExecutionService(
        projections,
        store,
        _handlers(slow),
        {"create_capability": validate_create_capability_admission} if validator else None,
    )
    continuations = WorkflowContinuationService(execution)
    return projections, store, execution, continuations, slow


def _start(arguments=None):
    return {
        "operation": "start",
        "workflow_path": "/workflows/capabilities/create-capability.md",
        "arguments": {"intent": "slow work"} if arguments is None else arguments,
    }


async def test_acknowledges_persisted_start_before_slow_continuation_finishes(tmp_path) -> None:
    projections, store, execution, continuations, slow = _service(tmp_path)
    continuations.start()
    adapter = WorkflowExecutionCapability(execution, continuations)

    result = await asyncio.wait_for(adapter.run("owner", _start(), SimpleNamespace(
        commit_sha=BASE, context=projections.context
    )), timeout=0.25)

    assert result["status"] == "accepted"
    persisted = store.get("owner", result["execution_id"])
    assert persisted is not None
    assert persisted.status in {"accepted", "running"}
    assert slow.started.wait(0.5)
    assert persisted.execution_id == result["execution_id"]

    slow.release.set()
    assert await continuations.wait_for_idle()
    assert store.get("owner", result["execution_id"]).status == "blocked"
    continuations.shutdown()


async def test_approved_resume_acknowledges_before_slow_continuation_finishes(tmp_path) -> None:
    projections, store, execution, continuations, slow = _service(tmp_path)
    definition = projections.definition
    paused = store.create(WorkflowExecution(
        execution_id="00000000-0000-4000-8000-000000000001",
        owner_id="owner",
        workflow_id=definition.summary.id,
        workflow_path=definition.summary.path.as_posix(),
        workflow_version=definition.summary.version,
        base_revision=BASE,
        invocation_arguments={},
        next_step_id=definition.runtime_plan.initial_step_id,
        status="stopped",
        step_results={},
        active_gate_id="development_result_review",
    ))
    continuations.start()
    adapter = WorkflowExecutionCapability(execution, continuations)

    result = await asyncio.wait_for(adapter.run("owner", {
        "operation": "resume", "execution_id": paused.execution_id,
        "gate_id": "development_result_review", "decision": "approved",
    }, SimpleNamespace(commit_sha=BASE, context=projections.context)), timeout=0.25)

    assert result["execution_id"] == paused.execution_id
    assert result["status"] == "accepted"
    assert slow.started.wait(0.5)
    slow.release.set()
    assert await continuations.wait_for_idle()
    assert store.get("owner", paused.execution_id).status == "blocked"
    continuations.shutdown()


async def test_invalid_create_capability_admission_creates_no_execution(tmp_path) -> None:
    projections, store, execution, _, _ = _service(tmp_path, validator=True)

    with pytest.raises(WorkflowExecutionError, match="invalid_arguments"):
        await execution.admit_start(
            "owner", projections.definition.summary.id, projections.definition.summary.path.as_posix(),
            {"intent": "missing required Create Capability fields"}, BASE,
        )

    assert store.list_for_owner("owner", 10) == []


async def test_accepted_execution_is_claimed_once_before_handler_side_effects(tmp_path) -> None:
    projections, store, execution, _, slow = _service(tmp_path)
    slow.release.set()
    admission = await execution.admit_start(
        "owner", projections.definition.summary.id, projections.definition.summary.path.as_posix(),
        {"intent": "slow work"}, BASE,
    )

    assert (await execution.advance_admission(admission)).status == "blocked"
    with pytest.raises(WorkflowExecutionError, match="already_claimed"):
        await execution.advance_admission(admission)
    assert store.get("owner", admission.execution.execution_id).status == "blocked"


async def test_scheduling_failure_never_returns_accepted(tmp_path) -> None:
    projections, store, execution, continuations, _ = _service(tmp_path)
    adapter = WorkflowExecutionCapability(execution, continuations)

    with pytest.raises(WorkflowCapabilityError, match="continuation_schedule_failed"):
        await adapter.run("owner", _start(), SimpleNamespace(commit_sha=BASE, context=projections.context))

    [persisted] = store.list_for_owner("owner", 10)
    assert persisted.status == "failed"
    assert persisted.error_code == "continuation_schedule_failed"
