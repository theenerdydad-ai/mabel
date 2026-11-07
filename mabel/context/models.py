"""Validated, bounded representation of a user MABEL context repository."""

from copy import deepcopy
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any


@dataclass(frozen=True)
class CapabilitySummary:
    id: str
    title: str
    path: PurePosixPath
    version: str
    schema_version: str | None = None

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "title": self.title,
            "path": self.path.as_posix(),
            "version": self.version,
        }


@dataclass(frozen=True)
class ContextManifest:
    start_instruction_path: PurePosixPath
    schema_manifest_path: PurePosixPath
    capability_manifest_path: PurePosixPath
    raw: dict[str, Any]
    generation: str = "legacy"
    workflow_manifest_path: PurePosixPath | None = None

    def as_dict(self) -> dict[str, object]:
        return dict(self.raw)


@dataclass(frozen=True)
class CapabilityDefinition:
    summary: CapabilitySummary
    executor_path: PurePosixPath | None
    callable_name: str | None
    tests_path: PurePosixPath | None
    contract: dict[str, object]
    input_contract: dict[str, object] | None = None
    output_contract: dict[str, object] | None = None
    effects: tuple[dict[str, object], ...] = ()
    execution_kind: str = "python_callable_v1"
    invocation: str = "no_arguments"
    runtime_binding: dict[str, object] | None = None

    def as_index_dict(self) -> dict[str, object]:
        """Project one validated, execution-eligible definition for live discovery.

        The invocation schema is deliberately projected from the same parsed
        contract that MABEL validates at execution time.  It is not rebuilt
        from capability-specific metadata or maintained by a bridge.
        """
        if self.input_contract is None:
            raise ValueError("Capability definition has no normalized input contract.")
        result: dict[str, object] = self.summary.as_dict()
        result["input_contract"] = deepcopy(self.input_contract)
        return result


@dataclass(frozen=True)
class WorkflowSummary:
    id: str
    title: str
    path: PurePosixPath
    version: str
    # AMIE workflow entries point at their authoritative Markdown procedure.
    # Unlike JSON capability artifacts, their Gateway-owned schema stamp may
    # not be materialized in repository-authored discovery metadata.
    schema_version: str | None = None

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "title": self.title,
            "path": self.path.as_posix(),
            "version": self.version,
        }


@dataclass(frozen=True)
class WorkflowRuntimeStep:
    """One bounded, document-declared workflow runtime action."""

    id: str
    action: str
    parameters: dict[str, object]
    transitions: dict[str, str]


@dataclass(frozen=True)
class WorkflowRuntimeGate:
    """A stable operator decision point declared by a workflow."""

    id: str
    purpose: str
    expected_decision: str
    continuation_step_id: str
    required_state: tuple[str, ...]


@dataclass(frozen=True)
class WorkflowRuntimePlan:
    """The small, governed execution contract embedded in a workflow document."""

    format_version: str
    initial_step_id: str
    state_contract: dict[str, object]
    steps: tuple[WorkflowRuntimeStep, ...]
    gates: tuple[WorkflowRuntimeGate, ...]


@dataclass(frozen=True)
class WorkflowDefinition:
    summary: WorkflowSummary
    procedure: tuple[dict[str, object], ...]
    approval_boundary: dict[str, object]
    runtime_plan: WorkflowRuntimePlan | None = None


@dataclass(frozen=True)
class LoadedContext:
    manifest: ContextManifest
    capabilities: tuple[CapabilitySummary, ...]
    governance_paths: tuple[PurePosixPath, ...] = ()
    workflows: tuple[WorkflowSummary, ...] = ()
    collection_errors: dict[str, str] | None = None
