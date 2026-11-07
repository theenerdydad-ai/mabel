"""Small, durable state models for the closed v1 workflow runtime."""

from dataclasses import dataclass
from datetime import datetime

from mabel.context.models import WorkflowRuntimePlan, WorkflowRuntimeStep


@dataclass(frozen=True)
class WorkflowExecution:
    execution_id: str
    owner_id: str
    workflow_id: str
    workflow_path: str
    workflow_version: str
    base_revision: str
    invocation_arguments: dict[str, object]
    next_step_id: str | None
    status: str
    step_results: dict[str, dict[str, object]]
    active_gate_id: str | None = None
    gate_decision: str | None = None
    proposal_id: str | None = None
    committed_revision: str | None = None
    error_code: str | None = None
    revision: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def expected_revision(self) -> str:
        return self.committed_revision or self.base_revision

    def as_dict(self) -> dict[str, object]:
        return {
            "execution_id": self.execution_id,
            "workflow_id": self.workflow_id,
            "workflow_path": self.workflow_path,
            "workflow_version": self.workflow_version,
            "base_revision": self.base_revision,
            "invocation_arguments": dict(self.invocation_arguments),
            "next_step_id": self.next_step_id,
            "status": self.status,
            "step_results": dict(self.step_results),
            "active_gate_id": self.active_gate_id,
            "gate_decision": self.gate_decision,
            "proposal_id": self.proposal_id,
            "committed_revision": self.committed_revision,
            "error_code": self.error_code,
            "created_at": self.created_at.isoformat() if self.created_at is not None else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at is not None else None,
        }


@dataclass(frozen=True)
class WorkflowActionInvocation:
    execution: WorkflowExecution
    plan: WorkflowRuntimePlan
    step: WorkflowRuntimeStep


@dataclass(frozen=True)
class WorkflowActionResult:
    """A bounded, structured action outcome used for declared transitions."""

    outcome: str
    evidence: dict[str, object]
    proposal_id: str | None = None
    committed_revision: str | None = None
