"""Owner-scoped, read-only workflow execution operations."""

from dataclasses import dataclass
from typing import Sequence

from mabel.state.workflow_executions import WorkflowExecutionStore, WorkflowExecutionStoreError
from mabel.workflows.models import WorkflowExecution


class WorkflowExecutionOperationsError(RuntimeError):
    """Workflow execution operations could not be completed safely."""


class WorkflowExecutionNotFoundError(WorkflowExecutionOperationsError):
    """The requested execution is not owned by the current owner."""


@dataclass(frozen=True)
class WorkflowExecutionList:
    executions: Sequence[WorkflowExecution]
    limit: int
    has_more: bool


class WorkflowExecutionOperationsService:
    """Read the existing workflow authority without changing execution state."""

    _DEFAULT_LIMIT = 50
    _MAX_LIMIT = 100

    def __init__(self, executions: WorkflowExecutionStore) -> None:
        self._executions = executions

    def list(self, owner_id: str, limit: int = _DEFAULT_LIMIT) -> WorkflowExecutionList:
        if not 1 <= limit <= self._MAX_LIMIT:
            raise WorkflowExecutionOperationsError("invalid_execution_limit")
        try:
            records = list(self._executions.list_for_owner(owner_id, limit + 1))
        except WorkflowExecutionStoreError as error:
            raise WorkflowExecutionOperationsError("execution_state_unavailable") from error
        return WorkflowExecutionList(records[:limit], limit, len(records) > limit)

    def get(self, owner_id: str, execution_id: str) -> WorkflowExecution:
        try:
            execution = self._executions.get(owner_id, execution_id)
        except WorkflowExecutionStoreError as error:
            raise WorkflowExecutionOperationsError("execution_state_unavailable") from error
        if execution is None:
            raise WorkflowExecutionNotFoundError("Workflow execution was not found.")
        return execution
