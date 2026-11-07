"""Validation for the intentionally small runtime-plan section in a workflow document."""

import json
import re
from collections.abc import Mapping
from typing import Any

from mabel.context.models import WorkflowRuntimeGate, WorkflowRuntimePlan, WorkflowRuntimeStep


class WorkflowRuntimePlanError(ValueError):
    """A document-owned workflow runtime plan is structurally or semantically invalid."""


_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*")
_PLAN_HEADING = "## Runtime Plan"
_PLAN_FENCE = re.compile(r"```mabel-runtime-plan\s*\n(.*?)\n```", re.DOTALL)
_TERMINALS = {
    "terminal:completed",
    "terminal:stopped",
    "terminal:blocked",
    "terminal:failed",
    "terminal:resource_limited",
}
_STATE_FIELDS = {
    "execution_id",
    "workflow_id",
    "workflow_version",
    "workflow_path",
    "base_revision",
    "invocation_arguments",
    "next_step_id",
    "step_results",
    "active_gate_id",
    "gate_decision",
    "proposal_id",
    "committed_revision",
}
_ACTION_OUTCOMES = {
    "prepare_capability_specification": {"success", "blocked", "failed", "resource_limited"},
    "invoke_governed_agent": {"success", "blocked", "failed", "resource_limited"},
    "evaluate_development_evidence": {"success", "blocked", "failed", "resource_limited"},
    "operator_gate": {"approved", "declined"},
    "verify_capability_proposal": {"success", "blocked", "failed", "resource_limited"},
    "create_repository_draft": {"success", "blocked", "failed", "resource_limited"},
    "commit_approved_draft": {"success", "blocked", "failed", "resource_limited"},
    "post_commit_capability_proof": {"success", "blocked", "failed", "resource_limited"},
}
_ACTION_PARAMETER_KEYS = {
    "prepare_capability_specification": {"input_ref", "supported_runtime", "governance_sources"},
    "invoke_governed_agent": {"agent", "brief"},
    "evaluate_development_evidence": {"evidence_ref"},
    "operator_gate": {"gate_id"},
    "verify_capability_proposal": {"evidence_ref", "allowed_resources"},
    "create_repository_draft": {"proposal_ref"},
    "commit_approved_draft": {"gate_id", "draft_ref"},
    "post_commit_capability_proof": {"commit_ref"},
}


def parse_workflow_runtime_plan(markdown: str) -> WorkflowRuntimePlan | None:
    """Return the optional document-owned runtime plan without interpreting prose."""
    section_start = markdown.find(_PLAN_HEADING)
    if section_start < 0:
        return None
    following_heading = markdown.find("\n## ", section_start + len(_PLAN_HEADING))
    section = markdown[section_start:following_heading if following_heading >= 0 else None]
    blocks = _PLAN_FENCE.findall(section)
    if len(blocks) != 1:
        raise WorkflowRuntimePlanError("Runtime Plan must contain exactly one mabel-runtime-plan block.")
    try:
        raw = json.loads(blocks[0])
    except json.JSONDecodeError as error:
        raise WorkflowRuntimePlanError("Runtime Plan must contain valid JSON.") from error
    return _plan(raw)


def _plan(raw: Any) -> WorkflowRuntimePlan:
    value = _mapping(raw, "Runtime Plan")
    _keys(value, {"format_version", "initial_step_id", "state_contract", "steps", "gates"}, "Runtime Plan")
    if value["format_version"] != "1.0":
        raise WorkflowRuntimePlanError("Runtime Plan format_version must be '1.0'.")
    initial_step_id = _identifier(value["initial_step_id"], "Runtime Plan initial_step_id")
    state_contract = _state_contract(value["state_contract"])
    steps = tuple(_step(item) for item in _list(value["steps"], "Runtime Plan steps"))
    gates = tuple(_gate(item) for item in _list(value["gates"], "Runtime Plan gates"))
    _validate_plan(initial_step_id, steps, gates)
    return WorkflowRuntimePlan("1.0", initial_step_id, state_contract, steps, gates)


def _state_contract(raw: Any) -> dict[str, object]:
    value = _mapping(raw, "Runtime Plan state_contract")
    _keys(value, {"required_fields", "on_revision_drift", "post_commit_provenance"}, "Runtime Plan state_contract")
    fields = _list(value["required_fields"], "Runtime Plan state_contract.required_fields")
    names = [_identifier(item, "Runtime Plan state field") for item in fields]
    if set(names) != _STATE_FIELDS or len(names) != len(_STATE_FIELDS):
        raise WorkflowRuntimePlanError("Runtime Plan state_contract must declare the complete v1 state set.")
    if value["on_revision_drift"] != "blocked":
        raise WorkflowRuntimePlanError("Runtime Plan must block on repository revision drift.")
    if value["post_commit_provenance"] != "explicit_transition":
        raise WorkflowRuntimePlanError("Runtime Plan must update provenance only through an explicit transition.")
    return dict(value)


def _step(raw: Any) -> WorkflowRuntimeStep:
    value = _mapping(raw, "Runtime Plan step")
    _keys(value, {"id", "action", "parameters", "transitions"}, "Runtime Plan step")
    step_id = _identifier(value["id"], "Runtime Plan step id")
    action = value["action"]
    if not isinstance(action, str) or action not in _ACTION_OUTCOMES:
        raise WorkflowRuntimePlanError("Runtime Plan step action is unsupported.")
    parameters = _mapping(value["parameters"], "Runtime Plan step parameters")
    if set(parameters) != _ACTION_PARAMETER_KEYS[action]:
        raise WorkflowRuntimePlanError("Runtime Plan step parameters do not match its governed action.")
    transitions = _mapping(value["transitions"], "Runtime Plan step transitions")
    if set(transitions) != _ACTION_OUTCOMES[action]:
        raise WorkflowRuntimePlanError("Runtime Plan step must declare every bounded action outcome.")
    parsed_transitions = {
        outcome: _target(target, "Runtime Plan transition") for outcome, target in transitions.items()
    }
    _validate_parameters(action, parameters)
    return WorkflowRuntimeStep(step_id, action, dict(parameters), parsed_transitions)


def _gate(raw: Any) -> WorkflowRuntimeGate:
    value = _mapping(raw, "Runtime Plan gate")
    _keys(value, {"id", "purpose", "expected_decision", "continuation_step_id", "required_state"}, "Runtime Plan gate")
    return WorkflowRuntimeGate(
        _identifier(value["id"], "Runtime Plan gate id"),
        _identifier(value["purpose"], "Runtime Plan gate purpose"),
        _identifier(value["expected_decision"], "Runtime Plan gate expected_decision"),
        _identifier(value["continuation_step_id"], "Runtime Plan gate continuation_step_id"),
        tuple(_identifier(item, "Runtime Plan gate required state") for item in _list(value["required_state"], "Runtime Plan gate required_state")),
    )


def _validate_plan(
    initial_step_id: str, steps: tuple[WorkflowRuntimeStep, ...], gates: tuple[WorkflowRuntimeGate, ...]
) -> None:
    step_ids = {step.id for step in steps}
    gate_ids = {gate.id for gate in gates}
    if len(step_ids) != len(steps):
        raise WorkflowRuntimePlanError("Runtime Plan step IDs must be unique.")
    if len(gate_ids) != len(gates):
        raise WorkflowRuntimePlanError("Runtime Plan gate IDs must be unique.")
    if initial_step_id not in step_ids:
        raise WorkflowRuntimePlanError("Runtime Plan initial_step_id must name a step.")
    for step in steps:
        for target in step.transitions.values():
            if target not in step_ids and target not in _TERMINALS:
                raise WorkflowRuntimePlanError("Runtime Plan transition target does not name a step or terminal outcome.")
        if step.action == "operator_gate":
            gate_id = step.parameters["gate_id"]
            gate = next((item for item in gates if item.id == gate_id), None)
            if gate is None:
                raise WorkflowRuntimePlanError("Runtime Plan stop gate must name a declared gate.")
            if step.transitions["approved"] != gate.continuation_step_id:
                raise WorkflowRuntimePlanError("Runtime Plan stop gate approval must use its declared continuation.")
        if step.action == "commit_approved_draft":
            gate_id = step.parameters["gate_id"]
            gate = next((item for item in gates if item.id == gate_id), None)
            if gate is None or gate.purpose != "exact_diff_approval" or gate.continuation_step_id != step.id:
                raise WorkflowRuntimePlanError("Runtime Plan commit must follow its exact-diff approval gate.")
    for gate in gates:
        if gate.continuation_step_id not in step_ids:
            raise WorkflowRuntimePlanError("Runtime Plan gate continuation must name a step.")
        if not gate.required_state:
            raise WorkflowRuntimePlanError("Runtime Plan gate must declare state required for safe resume.")


def _validate_parameters(action: str, parameters: Mapping[str, object]) -> None:
    if action == "prepare_capability_specification":
        if parameters["input_ref"] != "invocation.arguments" or parameters["supported_runtime"] != "context_python_callable":
            raise WorkflowRuntimePlanError("Capability specification action has invalid governed parameters.")
        _tokens(parameters["governance_sources"], "Runtime Plan governance source")
    elif action == "invoke_governed_agent":
        agent = _mapping(parameters["agent"], "Runtime Plan agent")
        _keys(agent, {"agent_class"}, "Runtime Plan agent")
        if agent["agent_class"] != "code_development":
            raise WorkflowRuntimePlanError("Runtime Plan agent must declare the supported code_development class.")
        brief = _mapping(parameters["brief"], "Runtime Plan development brief")
        _keys(brief, {"base_revision_ref", "specification_ref", "allowed_resources", "governance_sources", "acceptance_evidence", "prohibitions"}, "Runtime Plan development brief")
        for key in ("base_revision_ref", "specification_ref"):
            if not isinstance(brief[key], str) or not brief[key]:
                raise WorkflowRuntimePlanError("Runtime Plan development brief references must be non-empty strings.")
        for key in ("allowed_resources", "governance_sources", "acceptance_evidence", "prohibitions"):
            _tokens(brief[key], f"Runtime Plan development brief {key}")
    elif action == "operator_gate":
        _identifier(parameters["gate_id"], "Runtime Plan stop gate id")
    else:
        for value in parameters.values():
            if isinstance(value, str):
                if not value:
                    raise WorkflowRuntimePlanError("Runtime Plan action parameter must be non-empty.")
            else:
                _tokens(value, "Runtime Plan action parameter")


def _keys(value: Mapping[str, object], expected: set[str], location: str) -> None:
    if set(value) != expected:
        raise WorkflowRuntimePlanError(f"{location} has unsupported or missing fields.")


def _mapping(value: Any, location: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise WorkflowRuntimePlanError(f"{location} must be an object.")
    return value


def _list(value: Any, location: str) -> list[Any]:
    if not isinstance(value, list):
        raise WorkflowRuntimePlanError(f"{location} must be an array.")
    return value


def _identifier(value: Any, location: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise WorkflowRuntimePlanError(f"{location} must be a lowercase identifier.")
    return value


def _target(value: Any, location: str) -> str:
    if not isinstance(value, str) or (value not in _TERMINALS and not _IDENTIFIER.fullmatch(value)):
        raise WorkflowRuntimePlanError(f"{location} is invalid.")
    return value


def _tokens(value: Any, location: str) -> tuple[str, ...]:
    values = tuple(_identifier(item, location) for item in _list(value, location))
    if not values or len(set(values)) != len(values):
        raise WorkflowRuntimePlanError(f"{location} must contain unique identifiers.")
    return values
