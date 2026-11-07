from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from mabel.context.discovery import MabelContextDiscovery
from mabel.context.models import WorkflowDefinition, WorkflowRuntimeStep
from mabel.repository import LocalRepository
from mabel.state.database import SqliteDatabase
from mabel.state.workflow_executions import SqliteWorkflowExecutionStore
from mabel.workflows.execution import WorkflowExecutionError, WorkflowExecutionService
from mabel.workflows.models import WorkflowActionResult


BASE_REVISION = "a" * 40
COMMITTED_REVISION = "b" * 40


class StaticDiscovery:
    def __init__(self, definitions: dict[str, WorkflowDefinition]) -> None:
        self._definitions = definitions

    def load_workflow(self, workflow_id: str) -> WorkflowDefinition:
        return self._definitions[workflow_id]


class Projections:
    def __init__(self, discovery, revisions: list[str]) -> None:
        self.discovery = discovery
        self.revisions = revisions
        self.calls = 0

    async def synchronize(self, owner_id: str):
        assert owner_id == "owner"
        index = min(self.calls, len(self.revisions) - 1)
        self.calls += 1
        return SimpleNamespace(discovery=self.discovery, commit_sha=self.revisions[index])


class Handler:
    def __init__(self, action: str, result: WorkflowActionResult, calls: list[str], on_call=None) -> None:
        self.action = action
        self.result = result
        self.calls = calls
        self.on_call = on_call

    async def execute(self, invocation):
        self.calls.append(self.action)
        if self.on_call is not None:
            self.on_call(invocation)
        return self.result


def _definition(workflow_id: str = "create_capability") -> WorkflowDefinition:
    root = Path(__file__).resolve().parents[2] / "mabel-context"
    return MabelContextDiscovery(LocalRepository(root)).load_workflow(workflow_id)


def _service(tmp_path: Path, definitions: dict[str, WorkflowDefinition], handlers, revisions=None):
    database = SqliteDatabase(tmp_path / "state.db")
    database.initialize()
    projections = Projections(StaticDiscovery(definitions), revisions or [BASE_REVISION])
    return (
        WorkflowExecutionService(projections, SqliteWorkflowExecutionStore(database), handlers),
        projections,
        SqliteWorkflowExecutionStore(database),
    )


def _handlers(calls: list[str], projections: Projections | None = None):
    return {
        "prepare_capability_specification": Handler(
            "prepare_capability_specification", WorkflowActionResult("success", {"specification": "bounded"}), calls
        ),
        "invoke_governed_agent": Handler(
            "invoke_governed_agent",
            WorkflowActionResult("success", {
                "changed_files": ["capability_artifact"], "validation": ["focused_tests"],
                "assumptions": [], "blockers": [], "risks": [],
            }),
            calls,
        ),
        "evaluate_development_evidence": Handler(
            "evaluate_development_evidence", WorkflowActionResult("success", {"accepted": True}), calls
        ),
        "operator_gate": Handler("operator_gate", WorkflowActionResult("stopped", {"awaiting_operator": True}), calls),
        "verify_capability_proposal": Handler(
            "verify_capability_proposal", WorkflowActionResult("success", {"verified": True}), calls
        ),
        "create_repository_draft": Handler(
            "create_repository_draft", WorkflowActionResult("success", {"exact_diff": "bounded"}, proposal_id="draft-1"), calls
        ),
        "commit_approved_draft": Handler(
            "commit_approved_draft", WorkflowActionResult("success", {"committed": True}, committed_revision=COMMITTED_REVISION),
            calls,
            on_call=(lambda _: setattr(projections, "revisions", [COMMITTED_REVISION]) if projections else None),
        ),
        "post_commit_capability_proof": Handler(
            "post_commit_capability_proof", WorkflowActionResult("success", {"eligible": True, "proof": {"ok": True}}), calls
        ),
    }


async def test_create_capability_plan_starts_from_declared_step_and_preserves_arguments(tmp_path: Path) -> None:
    definition = _definition()
    calls: list[str] = []
    service, projections, _ = _service(tmp_path, {definition.summary.id: definition}, {})
    handlers = _handlers(calls, projections)
    handlers.update({
        "prepare_capability_specification": Handler(
            "prepare_capability_specification", WorkflowActionResult("blocked", {"reason": "needs_input"}), calls
        )
    })
    service._handlers = handlers

    execution = await service.start(
        "owner", definition.summary.id, "/" + definition.summary.path.as_posix(), {"intent": "Create one capability"}, BASE_REVISION
    )

    assert calls == ["prepare_capability_specification"]
    assert execution.status == "blocked"
    assert execution.base_revision == BASE_REVISION
    assert execution.invocation_arguments == {"intent": "Create one capability"}
    assert execution.step_results["prepare_capability_specification"]["outcome"] == "blocked"


@pytest.mark.parametrize("outcome", ["blocked", "failed", "resource_limited"])
async def test_bounded_action_outcomes_preserve_declared_terminal_status(tmp_path: Path, outcome: str) -> None:
    definition = _definition()
    calls: list[str] = []
    handlers = _handlers(calls)
    handlers.update({
        "prepare_capability_specification": Handler(
            "prepare_capability_specification", WorkflowActionResult(outcome, {"status": outcome}), calls
        )
    })
    service, _, _ = _service(tmp_path, {definition.summary.id: definition}, handlers)

    execution = await service.start("owner", definition.summary.id, definition.summary.path.as_posix(), {}, BASE_REVISION)

    assert execution.status == outcome
    assert calls == ["prepare_capability_specification"]


async def test_full_create_capability_plan_resumes_across_both_governed_gates(tmp_path: Path) -> None:
    definition = _definition()
    calls: list[str] = []
    database = SqliteDatabase(tmp_path / "state.db")
    database.initialize()
    store = SqliteWorkflowExecutionStore(database)
    projections = Projections(StaticDiscovery({definition.summary.id: definition}), [BASE_REVISION])
    first = WorkflowExecutionService(projections, store, _handlers(calls, projections))

    stopped_for_review = await first.start(
        "owner", definition.summary.id, definition.summary.path.as_posix(), {"intent": "Create echo-like capability"}, BASE_REVISION
    )
    assert stopped_for_review.status == "stopped"
    assert stopped_for_review.active_gate_id == "development_result_review"
    assert stopped_for_review.proposal_id is None
    assert calls == [
        "prepare_capability_specification", "invoke_governed_agent", "evaluate_development_evidence", "operator_gate",
    ]

    # A new service instance proves stop state is durable instead of in-memory.
    second = WorkflowExecutionService(projections, store, _handlers(calls, projections))
    stopped_for_diff = await second.resume(
        "owner", stopped_for_review.execution_id, "development_result_review", "approved"
    )
    assert stopped_for_diff.status == "stopped"
    assert stopped_for_diff.active_gate_id == "exact_diff_approval"
    assert stopped_for_diff.proposal_id == "draft-1"
    assert calls.count("prepare_capability_specification") == 1
    assert calls.count("invoke_governed_agent") == 1

    completed = await second.resume(
        "owner", stopped_for_diff.execution_id, "exact_diff_approval", "approved"
    )
    assert completed.status == "completed"
    assert completed.committed_revision == COMMITTED_REVISION
    assert completed.expected_revision == COMMITTED_REVISION
    assert completed.step_results["post_commit_capability_proof"]["evidence"]["proof"] == {"ok": True}
    assert calls.count("commit_approved_draft") == 1
    assert calls.count("operator_gate") == 2
    with pytest.raises(WorkflowExecutionError, match="invalid_execution_state"):
        await second.resume("owner", completed.execution_id, "exact_diff_approval", "approved")


async def test_gate_decisions_are_bound_to_the_active_gate(tmp_path: Path) -> None:
    definition = _definition()
    calls: list[str] = []
    database = SqliteDatabase(tmp_path / "state.db")
    database.initialize()
    projections = Projections(StaticDiscovery({definition.summary.id: definition}), [BASE_REVISION])
    service = WorkflowExecutionService(projections, SqliteWorkflowExecutionStore(database), _handlers(calls, projections))
    stopped = await service.start("owner", definition.summary.id, definition.summary.path.as_posix(), {}, BASE_REVISION)

    with pytest.raises(WorkflowExecutionError, match="invalid_gate_decision"):
        await service.resume("owner", stopped.execution_id, "exact_diff_approval", "approved")
    after_review = await service.resume("owner", stopped.execution_id, "development_result_review", "approved")

    assert after_review.active_gate_id == "exact_diff_approval"
    assert "commit_approved_draft" not in calls


async def test_revision_drift_blocks_without_silently_adopting_new_context(tmp_path: Path) -> None:
    definition = _definition()
    calls: list[str] = []
    handlers = _handlers(calls)
    handlers.update({
        "prepare_capability_specification": Handler(
            "prepare_capability_specification", WorkflowActionResult("success", {}), calls
        )
    })
    service, _, _ = _service(
        tmp_path, {definition.summary.id: definition}, handlers, [BASE_REVISION, "c" * 40]
    )

    execution = await service.start("owner", definition.summary.id, definition.summary.path.as_posix(), {}, BASE_REVISION)

    assert execution.status == "blocked"
    assert execution.error_code == "revision_drift"
    assert calls == []


async def test_non_runtime_workflow_and_runtime_defects_are_rejected_without_execution(tmp_path: Path) -> None:
    descriptive = _definition("create_workflow")
    service, _, _ = _service(tmp_path, {descriptive.summary.id: descriptive}, {})

    with pytest.raises(WorkflowExecutionError, match="workflow_not_executable"):
        await service.start("owner", descriptive.summary.id, descriptive.summary.path.as_posix(), {}, BASE_REVISION)

    executable = _definition()
    assert executable.runtime_plan is not None
    malformed_step = replace(executable.runtime_plan.steps[0], action="unknown_action")
    malformed_plan = replace(executable.runtime_plan, steps=(malformed_step, *executable.runtime_plan.steps[1:]))
    malformed = replace(executable, runtime_plan=malformed_plan)
    service, _, _ = _service(tmp_path, {malformed.summary.id: malformed}, {})

    with pytest.raises(WorkflowExecutionError, match="workflow_not_executable"):
        await service.start("owner", malformed.summary.id, malformed.summary.path.as_posix(), {}, BASE_REVISION)


async def test_commit_requires_exact_diff_gate_decision_even_for_a_tampered_persisted_state(tmp_path: Path) -> None:
    definition = _definition()
    assert definition.runtime_plan is not None
    plan = definition.runtime_plan
    commit_step = next(step for step in plan.steps if step.action == "commit_approved_draft")
    start_step = replace(commit_step, id="commit_first", transitions={"success": "terminal:completed", "blocked": "terminal:blocked", "failed": "terminal:failed", "resource_limited": "terminal:resource_limited"})
    tampered_plan = replace(plan, initial_step_id="commit_first", steps=(start_step,))
    tampered = replace(definition, runtime_plan=tampered_plan)
    calls: list[str] = []
    handlers = {"commit_approved_draft": Handler(
        "commit_approved_draft", WorkflowActionResult("success", {}, committed_revision=COMMITTED_REVISION), calls
    )}
    service, _, _ = _service(tmp_path, {tampered.summary.id: tampered}, handlers)

    execution = await service.start("owner", tampered.summary.id, tampered.summary.path.as_posix(), {}, BASE_REVISION)

    assert execution.status == "failed"
    assert execution.error_code == "invalid_gate_decision"
    assert calls == []
