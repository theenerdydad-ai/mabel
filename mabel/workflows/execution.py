"""Deterministic execution core for the closed, governed workflow-plan v1."""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Callable, Protocol
from uuid import uuid4

from mabel.context.models import WorkflowDefinition, WorkflowRuntimePlan, WorkflowRuntimeStep
from mabel.state.workflow_executions import WorkflowExecutionStore, WorkflowExecutionStoreError
from mabel.workflows.models import WorkflowActionInvocation, WorkflowActionResult, WorkflowExecution


class WorkflowExecutionError(ValueError):
    """A stable internal workflow-execution rejection."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class WorkflowProjectionSynchronizer(Protocol):
    async def synchronize(self, owner_id: str): ...


class WorkflowActionHandler(Protocol):
    async def execute(self, invocation: WorkflowActionInvocation) -> WorkflowActionResult: ...


WorkflowAdmissionValidator = Callable[[Mapping[str, object], str], None]


@dataclass(frozen=True)
class WorkflowAdmission:
    """A durably accepted execution and the validated in-memory plan to run."""

    execution: WorkflowExecution
    plan: WorkflowRuntimePlan


_ACTIONS = frozenset({
    "prepare_capability_specification",
    "invoke_governed_agent",
    "evaluate_development_evidence",
    "operator_gate",
    "verify_capability_proposal",
    "create_repository_draft",
    "commit_approved_draft",
    "post_commit_capability_proof",
})


class WorkflowExecutionService:
    """Start and resume one validated plan using only its declared action graph."""

    def __init__(
        self,
        projections: WorkflowProjectionSynchronizer,
        executions: WorkflowExecutionStore,
        handlers: Mapping[str, WorkflowActionHandler],
        admission_validators: Mapping[str, WorkflowAdmissionValidator] | None = None,
    ) -> None:
        if set(handlers) - _ACTIONS:
            raise ValueError("Workflow execution handlers include an unsupported action.")
        self._projections = projections
        self._executions = executions
        self._handlers = dict(handlers)
        self._admission_validators = dict(admission_validators or {})

    async def start(
        self,
        owner_id: str,
        workflow_id: str,
        workflow_path: str,
        arguments: Mapping[str, object],
        base_revision: str,
    ) -> WorkflowExecution:
        admission = await self.admit_start(owner_id, workflow_id, workflow_path, arguments, base_revision)
        return await self.advance_admission(admission)

    async def admit_start(
        self,
        owner_id: str,
        workflow_id: str,
        workflow_path: str,
        arguments: Mapping[str, object],
        base_revision: str,
    ) -> WorkflowAdmission:
        if (
            not isinstance(workflow_id, str) or not workflow_id
            or not isinstance(workflow_path, str) or not workflow_path
            or not isinstance(arguments, Mapping) or not isinstance(base_revision, str) or not base_revision
        ):
            raise WorkflowExecutionError("invalid_arguments")
        projection = await self._projection(owner_id)
        if projection.commit_sha != base_revision:
            raise WorkflowExecutionError("revision_drift")
        definition = self._workflow(projection, workflow_id, workflow_path)
        plan = definition.runtime_plan
        assert plan is not None
        self._validate_admission_plan(plan)
        validator = self._admission_validators.get(definition.summary.id)
        if validator is not None:
            try:
                validator(arguments, base_revision)
            except ValueError as error:
                raise WorkflowExecutionError("invalid_arguments") from error
        try:
            execution = self._executions.create(WorkflowExecution(
                execution_id=str(uuid4()), owner_id=owner_id, workflow_id=definition.summary.id,
                workflow_path=definition.summary.path.as_posix(), workflow_version=definition.summary.version,
                base_revision=base_revision, invocation_arguments=dict(arguments),
                next_step_id=plan.initial_step_id, status="accepted", step_results={},
            ))
        except WorkflowExecutionStoreError as error:
            raise WorkflowExecutionError("persistence_failed") from error
        return WorkflowAdmission(execution, plan)

    async def resume(
        self, owner_id: str, execution_id: str, gate_id: str, decision: str
    ) -> WorkflowExecution:
        result = await self.admit_resume(owner_id, execution_id, gate_id, decision)
        if isinstance(result, WorkflowAdmission):
            return await self.advance_admission(result)
        return result

    async def admit_resume(
        self, owner_id: str, execution_id: str, gate_id: str, decision: str
    ) -> WorkflowAdmission | WorkflowExecution:
        execution = self._execution(owner_id, execution_id)
        if execution.status != "stopped" or execution.active_gate_id is None:
            raise WorkflowExecutionError("invalid_execution_state")
        if gate_id != execution.active_gate_id or decision not in {"approved", "declined"}:
            raise WorkflowExecutionError("invalid_gate_decision")
        projection = await self._projection(owner_id)
        definition = self._workflow(projection, execution.workflow_id, execution.workflow_path)
        plan = definition.runtime_plan
        assert plan is not None
        self._validate_admission_plan(plan)
        execution = await self._block_on_drift(execution, projection.commit_sha)
        if execution.status == "blocked":
            return execution
        if decision == "declined":
            return self._persist(replace(
                execution, status="stopped", next_step_id=None, gate_decision=decision,
                active_gate_id=None, error_code=None,
            ))
        execution = self._persist(replace(
            execution, status="accepted", active_gate_id=None, gate_decision=decision, error_code=None,
        ))
        return WorkflowAdmission(execution, plan)

    async def advance_admission(self, admission: WorkflowAdmission) -> WorkflowExecution:
        """Claim a submitted admission before dispatching the unchanged runtime loop."""
        execution = self._claim(admission.execution)
        return await self._advance(execution, admission.plan)

    def fail_admission(self, admission: WorkflowAdmission, code: str) -> WorkflowExecution:
        """Record a handoff failure without ever claiming the workflow as running."""
        if not isinstance(code, str) or not code:
            raise WorkflowExecutionError("invalid_arguments")
        return self._persist(replace(admission.execution, status="failed", next_step_id=None, error_code=code))

    def fail_continuation(self, owner_id: str, execution_id: str, code: str) -> WorkflowExecution | None:
        """Best-effort terminal state for an exception outside normal action handling."""
        execution = self._execution(owner_id, execution_id)
        if execution.status not in {"accepted", "running"}:
            return execution
        return self._persist(replace(execution, status="failed", next_step_id=None, error_code=code))

    def get(self, owner_id: str, execution_id: str) -> WorkflowExecution:
        return self._execution(owner_id, execution_id)

    async def _advance(self, execution: WorkflowExecution, plan: WorkflowRuntimePlan) -> WorkflowExecution:
        while execution.status == "running":
            projection = await self._projection(execution.owner_id)
            execution = await self._block_on_drift(execution, projection.commit_sha)
            if execution.status != "running":
                return execution
            if execution.next_step_id is None:
                return self._persist(replace(execution, status="failed", error_code="invalid_transition"))
            step = next((item for item in plan.steps if item.id == execution.next_step_id), None)
            if step is None:
                return self._persist(replace(execution, status="failed", error_code="invalid_transition"))
            if step.action not in _ACTIONS:
                return self._persist(replace(execution, status="failed", error_code="unsupported_action"))
            handler = self._handlers.get(step.action)
            if handler is None:
                return self._persist(replace(execution, status="failed", error_code="unsupported_action"))
            if step.action == "commit_approved_draft" and (
                execution.gate_decision != "approved" or not execution.proposal_id
            ):
                return self._persist(replace(execution, status="failed", error_code="invalid_gate_decision"))
            try:
                result = await handler.execute(WorkflowActionInvocation(execution, plan, step))
            except Exception:
                return self._persist(replace(execution, status="failed", error_code="handler_failed"))
            if not isinstance(result, WorkflowActionResult) or not isinstance(result.evidence, dict):
                return self._persist(replace(execution, status="failed", error_code="invalid_action_result"))
            if step.action == "operator_gate":
                execution = self._record_step(execution, step, result)
                if result.outcome != "stopped":
                    return self._persist(replace(execution, status="failed", error_code="invalid_action_result"))
                gate_id = step.parameters["gate_id"]
                return self._persist(replace(
                    execution, status="stopped", active_gate_id=str(gate_id), gate_decision=None,
                    next_step_id=step.transitions["approved"], error_code=None,
                ))
            if result.outcome not in step.transitions:
                return self._persist(replace(execution, status="failed", error_code="invalid_transition"))
            if result.proposal_id is not None and step.action != "create_repository_draft":
                return self._persist(replace(execution, status="failed", error_code="invalid_action_result"))
            if result.proposal_id is not None and (not isinstance(result.proposal_id, str) or not result.proposal_id):
                return self._persist(replace(execution, status="failed", error_code="invalid_action_result"))
            if result.committed_revision is not None and step.action != "commit_approved_draft":
                return self._persist(replace(execution, status="failed", error_code="invalid_action_result"))
            if step.action == "commit_approved_draft":
                if not _commit_sha(result.committed_revision):
                    return self._persist(replace(execution, status="failed", error_code="invalid_action_result"))
            execution = self._record_step(execution, step, result)
            execution = replace(
                execution,
                proposal_id=result.proposal_id or execution.proposal_id,
                committed_revision=result.committed_revision or execution.committed_revision,
                error_code=None,
            )
            target = step.transitions[result.outcome]
            if target.startswith("terminal:"):
                return self._persist(replace(execution, status=target.removeprefix("terminal:"), next_step_id=None))
            if not any(item.id == target for item in plan.steps):
                return self._persist(replace(execution, status="failed", error_code="invalid_transition"))
            execution = self._persist(replace(execution, next_step_id=target, status="running"))
        return execution

    async def _block_on_drift(self, execution: WorkflowExecution, actual_revision: str) -> WorkflowExecution:
        if actual_revision == execution.expected_revision:
            return execution
        return self._persist(replace(
            execution, status="blocked", next_step_id=None, active_gate_id=None, error_code="revision_drift",
        ))

    @staticmethod
    def _record_step(
        execution: WorkflowExecution, step: WorkflowRuntimeStep, result: WorkflowActionResult
    ) -> WorkflowExecution:
        results = dict(execution.step_results)
        evidence = dict(result.evidence)
        if result.proposal_id is not None:
            evidence["proposal_id"] = result.proposal_id
        if result.committed_revision is not None:
            evidence["committed_revision"] = result.committed_revision
        results[step.id] = {"outcome": result.outcome, "evidence": evidence}
        return replace(execution, step_results=results)

    def _execution(self, owner_id: str, execution_id: str) -> WorkflowExecution:
        try:
            execution = self._executions.get(owner_id, execution_id)
        except WorkflowExecutionStoreError as error:
            raise WorkflowExecutionError("persistence_failed") from error
        if execution is None:
            raise WorkflowExecutionError("invalid_execution_id")
        return execution

    def _persist(self, execution: WorkflowExecution) -> WorkflowExecution:
        try:
            return self._executions.update(execution)
        except WorkflowExecutionStoreError as error:
            raise WorkflowExecutionError("persistence_failed") from error

    def _claim(self, execution: WorkflowExecution) -> WorkflowExecution:
        try:
            claimed = self._executions.claim(execution)
        except WorkflowExecutionStoreError as error:
            raise WorkflowExecutionError("persistence_failed") from error
        if claimed is None:
            raise WorkflowExecutionError("already_claimed")
        return claimed

    def _validate_admission_plan(self, plan: WorkflowRuntimePlan) -> None:
        if any(step.action not in _ACTIONS or step.action not in self._handlers for step in plan.steps):
            raise WorkflowExecutionError("workflow_not_executable")

    async def _projection(self, owner_id: str):
        try:
            return await self._projections.synchronize(owner_id)
        except Exception as error:
            raise WorkflowExecutionError("context_unavailable") from error

    @staticmethod
    def _workflow(projection, workflow_id: str, workflow_path: str) -> WorkflowDefinition:
        try:
            definition = projection.discovery.load_workflow(workflow_id)
        except Exception as error:
            raise WorkflowExecutionError("invalid_workflow") from error
        expected = definition.summary.path.as_posix()
        supplied = PurePosixPath(workflow_path.lstrip("/")).as_posix()
        if supplied != expected:
            raise WorkflowExecutionError("invalid_workflow")
        if definition.runtime_plan is None:
            raise WorkflowExecutionError("workflow_not_executable")
        return definition


def _commit_sha(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(character in "0123456789abcdef" for character in value)
    )
