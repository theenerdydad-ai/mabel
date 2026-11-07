"""The narrow capability-facing adapter for governed workflow execution."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Protocol

from mabel.workflows.continuation import WorkflowContinuationError
from mabel.workflows.execution import WorkflowAdmission, WorkflowExecutionError, WorkflowExecutionService
from mabel.workflows.models import WorkflowExecution


class WorkflowCapabilityError(ValueError):
    """A stable request rejection at the workflow capability boundary."""


class WorkflowProjection(Protocol):
    commit_sha: str
    context: object


class WorkflowContinuation(Protocol):
    def submit(self, admission: WorkflowAdmission) -> None: ...


class WorkflowExecutionCapability:
    """Adapt one declared capability operation to the existing execution service.

    This class deliberately owns no workflow methodology, handlers, durable
    state, or repository operations.  It resolves a caller's authoritative
    workflow path against the synchronized projection and delegates to the
    already composed execution service.
    """

    def __init__(
        self, execution: WorkflowExecutionService, continuations: WorkflowContinuation | None = None
    ) -> None:
        self._execution = execution
        self._continuations = continuations

    async def run(
        self, owner_id: str, arguments: Mapping[str, object], projection: WorkflowProjection
    ) -> dict[str, object]:
        operation = arguments.get("operation")
        if operation == "start":
            workflow_path = _path(arguments.get("workflow_path"))
            workflow = _workflow_for_path(projection, workflow_path)
            invocation = arguments.get("arguments")
            if not isinstance(invocation, Mapping):
                raise WorkflowCapabilityError("invalid_arguments")
            try:
                admission = await self._execution.admit_start(
                    owner_id,
                    workflow.id,
                    workflow.path.as_posix(),
                    invocation,
                    projection.commit_sha,
                )
                if self._continuations is None:
                    execution = await self._execution.advance_admission(admission)
                else:
                    self._submit(admission)
                    # This is the durable admission snapshot. The local runner
                    # may already have claimed it by the time the transport is sent.
                    execution = admission.execution
            except WorkflowExecutionError as error:
                raise WorkflowCapabilityError(error.code) from error
            except WorkflowContinuationError as error:
                raise WorkflowCapabilityError("continuation_schedule_failed") from error
            return serialize_workflow_execution(execution)
        if operation == "resume":
            execution_id = arguments.get("execution_id")
            gate_id = arguments.get("gate_id")
            decision = arguments.get("decision")
            if not all(isinstance(value, str) and value for value in (execution_id, gate_id, decision)):
                raise WorkflowCapabilityError("invalid_arguments")
            try:
                result = await self._execution.admit_resume(owner_id, execution_id, gate_id, decision)
                if isinstance(result, WorkflowAdmission):
                    if self._continuations is None:
                        execution = await self._execution.advance_admission(result)
                    else:
                        self._submit(result)
                        execution = result.execution
                else:
                    execution = result
            except WorkflowExecutionError as error:
                raise WorkflowCapabilityError(error.code) from error
            except WorkflowContinuationError as error:
                raise WorkflowCapabilityError("continuation_schedule_failed") from error
            return serialize_workflow_execution(execution)
        raise WorkflowCapabilityError("invalid_arguments")

    def _submit(self, admission: WorkflowAdmission) -> None:
        assert self._continuations is not None
        try:
            self._continuations.submit(admission)
        except WorkflowContinuationError:
            try:
                self._execution.fail_admission(admission, "continuation_schedule_failed")
            except WorkflowExecutionError:
                pass
            raise


def serialize_workflow_execution(execution: WorkflowExecution) -> dict[str, object]:
    """Publish only the bounded review/result slices needed by AMIE.

    Evidence has already been normalized by the production handlers.  This
    avoids exposing caller arguments or internal state-machine transition
    fields while retaining both review gates, the governed draft, and proof.
    """
    steps = execution.step_results
    review: dict[str, object] = {}
    development = _evidence(steps.get("evaluate_development_evidence"))
    if development is not None:
        review["development"] = development.get("development", development)
    draft = _evidence(steps.get("create_repository_draft"))
    if draft is not None:
        review["draft"] = draft.get("draft", draft)
    proof = _evidence(steps.get("post_commit_capability_proof"))
    response: dict[str, object] = {
        "execution_id": execution.execution_id,
        "workflow": {
            "id": execution.workflow_id,
            "path": "/" + execution.workflow_path,
            "version": execution.workflow_version,
        },
        "base_revision": execution.base_revision,
        "status": execution.status,
        "active_gate": {"id": execution.active_gate_id} if execution.active_gate_id else None,
        "proposal_id": execution.proposal_id,
        "committed_revision": execution.committed_revision,
        "review": review,
        "proof": proof,
        "failure": {"code": execution.error_code} if execution.error_code else None,
    }
    return response


def _workflow_for_path(projection: WorkflowProjection, workflow_path: str):
    context = projection.context
    workflows = getattr(context, "workflows", ())
    candidate = next((workflow for workflow in workflows if workflow.path.as_posix() == workflow_path), None)
    if candidate is None:
        raise WorkflowCapabilityError("invalid_workflow")
    return candidate


def _path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise WorkflowCapabilityError("invalid_arguments")
    path = PurePosixPath(value.lstrip("/"))
    if path.is_absolute() or ".." in path.parts:
        raise WorkflowCapabilityError("invalid_arguments")
    return path.as_posix()


def _evidence(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    evidence = value.get("evidence")
    return dict(evidence) if isinstance(evidence, Mapping) else None
