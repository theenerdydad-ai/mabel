"""Production action handlers for the closed, document-owned workflow plan."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from jsonschema import Draft202012Validator, SchemaError

from mabel.services.amie_context import AmieContextOperationService
from mabel.services.capabilities import CapabilityService
from mabel.services.development_agents import (
    DevelopmentAgentError,
    DevelopmentAgentRequest,
    DevelopmentAgentService,
)
from mabel.services.development_workspace import DevelopmentWorkspaceError, DevelopmentWorkspaceService
from mabel.workflows.models import WorkflowActionInvocation, WorkflowActionResult


_CAPABILITY_ID = re.compile(r"[a-z][a-z0-9_-]*")
_COMMIT = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class _Handler:
    callback: Callable[[WorkflowActionInvocation], Awaitable[WorkflowActionResult]]

    async def execute(self, invocation: WorkflowActionInvocation) -> WorkflowActionResult:
        return await self.callback(invocation)


class ProductionWorkflowHandlers:
    """Explicit dependency-injected implementations for every v1 action."""

    def __init__(
        self,
        projections: object,
        workspaces: DevelopmentWorkspaceService,
        agents: DevelopmentAgentService | None,
        amie_context: AmieContextOperationService,
        capabilities: CapabilityService,
    ) -> None:
        self._projections = projections
        self._workspaces = workspaces
        self._agents = agents
        self._amie_context = amie_context
        self._capabilities = capabilities

    def handlers(self) -> dict[str, _Handler]:
        return {
            "prepare_capability_specification": _Handler(self.prepare_capability_specification),
            "invoke_governed_agent": _Handler(self.invoke_governed_agent),
            "evaluate_development_evidence": _Handler(self.evaluate_development_evidence),
            "operator_gate": _Handler(self.operator_gate),
            "verify_capability_proposal": _Handler(self.verify_capability_proposal),
            "create_repository_draft": _Handler(self.create_repository_draft),
            "commit_approved_draft": _Handler(self.commit_approved_draft),
            "post_commit_capability_proof": _Handler(self.post_commit_capability_proof),
        }

    async def prepare_capability_specification(self, invocation: WorkflowActionInvocation) -> WorkflowActionResult:
        try:
            specification = build_capability_specification(
                invocation.execution.invocation_arguments, invocation.execution.base_revision
            )
            return WorkflowActionResult("success", {"specification": specification})
        except ValueError as error:
            return WorkflowActionResult("blocked", {"reason": str(error)})

    async def invoke_governed_agent(self, invocation: WorkflowActionInvocation) -> WorkflowActionResult:
        if self._agents is None:
            return WorkflowActionResult("blocked", {"reason": "development_agent_not_configured"})
        workspace = None
        try:
            specification = _specification(_result(invocation, str(invocation.step.parameters["brief"]["specification_ref"])))
            projection = await self._projection(invocation.execution.owner_id)
            if projection.commit_sha != invocation.execution.base_revision:
                return WorkflowActionResult("blocked", {"reason": "revision_drift"})
            allowed = _allowed_paths(specification)
            workspace = await self._workspaces.create(
                invocation.execution.owner_id, invocation.execution.base_revision, allowed,
                execution_id=invocation.execution.execution_id,
            )
            brief_parameters = _mapping(invocation.step.parameters["brief"])
            brief = {
                "base_revision": invocation.execution.base_revision,
                "specification": specification,
                "allowed_paths": allowed,
                "governance_paths": [str(path) for path in projection.context.governance_paths],
                "required_tests": list(brief_parameters["acceptance_evidence"]),
                "prohibitions": list(brief_parameters["prohibitions"]),
            }
            result = await self._agents.run(DevelopmentAgentRequest(
                agent_class=_mapping(invocation.step.parameters["agent"])["agent_class"], workspace=workspace,
                base_revision=invocation.execution.base_revision, brief=brief,
                acceptance=_mapping(specification["acceptance"]), execution_id=invocation.execution.execution_id,
            ))
            evidence = dict(result.evidence)
            evidence["specification"] = specification
            evidence["allowed_paths"] = allowed
            return WorkflowActionResult(result.status, evidence)
        except DevelopmentWorkspaceError as error:
            return WorkflowActionResult("blocked", {"reason": error.code})
        except DevelopmentAgentError as error:
            return WorkflowActionResult("failed", {"reason": error.code})
        except Exception:
            return WorkflowActionResult("failed", {"reason": "development_agent_failed"})
        finally:
            if workspace is not None:
                try:
                    self._workspaces.cleanup(workspace)
                except Exception:
                    pass

    async def evaluate_development_evidence(self, invocation: WorkflowActionInvocation) -> WorkflowActionResult:
        try:
            evidence = _result(invocation, str(invocation.step.parameters["evidence_ref"]))
            changes = _mapping(evidence.get("changes"))
            changed = _strings(changes.get("changed_files"))
            files = changes.get("proposed_files")
            if not changed or not isinstance(files, list) or not files:
                return WorkflowActionResult("failed", {"reason": "missing_development_changes"})
            if not _mapping(evidence.get("validation")):
                return WorkflowActionResult("failed", {"reason": "missing_validation_evidence"})
            specification = _specification(evidence)
            if any(not _allowed_change(path, specification) for path in changed):
                return WorkflowActionResult("failed", {"reason": "out_of_scope_changes"})
            return WorkflowActionResult("success", {"specification": specification, "development": evidence})
        except ValueError as error:
            return WorkflowActionResult("failed", {"reason": str(error)})

    async def operator_gate(self, invocation: WorkflowActionInvocation) -> WorkflowActionResult:
        return WorkflowActionResult("stopped", {"gate_id": str(invocation.step.parameters["gate_id"])})

    async def verify_capability_proposal(self, invocation: WorkflowActionInvocation) -> WorkflowActionResult:
        try:
            evaluation = _result(invocation, str(invocation.step.parameters["evidence_ref"]))
            specification = _specification(evaluation)
            development = _mapping(evaluation.get("development"))
            changes = _mapping(development.get("changes"))
            replacements = _replacements(changes.get("proposed_files"))
            _verify_replacements(specification, replacements)
            return WorkflowActionResult("success", {
                "specification": specification, "replacements": replacements,
                "validation": {"independent_proposal_verification": "passed"},
            })
        except ValueError as error:
            return WorkflowActionResult("failed", {"reason": str(error)})

    async def create_repository_draft(self, invocation: WorkflowActionInvocation) -> WorkflowActionResult:
        try:
            projection = await self._projection(invocation.execution.owner_id)
            if projection.commit_sha != invocation.execution.base_revision:
                return WorkflowActionResult("blocked", {"reason": "revision_drift"})
            proposal = _result(invocation, str(invocation.step.parameters["proposal_ref"]))
            specification = _specification(proposal)
            replacements = _replacements(proposal.get("replacements"))
            response = await self._amie_context.run(invocation.execution.owner_id, "create_draft", {
                "files": [{"path": "/" + item["path"], "content": item["content"]} for item in replacements],
                "commit_message": f"Create capability {specification['capability_id']}",
            })
            draft_id = response.get("draft_id")
            if not isinstance(draft_id, str) or not draft_id:
                return WorkflowActionResult("failed", {"reason": "invalid_draft_response"})
            return WorkflowActionResult("success", {
                "base_revision": invocation.execution.base_revision, "draft": _safe_draft(response),
                "specification": specification,
            }, proposal_id=draft_id)
        except Exception:
            return WorkflowActionResult("failed", {"reason": "draft_creation_failed"})

    async def commit_approved_draft(self, invocation: WorkflowActionInvocation) -> WorkflowActionResult:
        try:
            if invocation.execution.gate_decision != "approved" or not invocation.execution.proposal_id:
                return WorkflowActionResult("failed", {"reason": "exact_diff_approval_required"})
            gate_id = str(invocation.step.parameters["gate_id"])
            if not _gate_was_reached(invocation, gate_id):
                return WorkflowActionResult("failed", {"reason": "exact_diff_approval_required"})
            projection = await self._projection(invocation.execution.owner_id)
            if projection.commit_sha != invocation.execution.base_revision:
                return WorkflowActionResult("blocked", {"reason": "revision_drift"})
            draft = await self._amie_context.run(invocation.execution.owner_id, "get_draft", {"draft_id": invocation.execution.proposal_id})
            if draft.get("draft_id") != invocation.execution.proposal_id or draft.get("status") != "pending":
                return WorkflowActionResult("blocked", {"reason": "draft_not_pending"})
            response = await self._amie_context.run(invocation.execution.owner_id, "commit_draft", {"draft_id": invocation.execution.proposal_id})
            revision = response.get("github_commit_sha")
            if not isinstance(revision, str) or not _COMMIT.fullmatch(revision):
                return WorkflowActionResult("failed", {"reason": "invalid_commit_response"})
            return WorkflowActionResult("success", {"draft_id": invocation.execution.proposal_id}, committed_revision=revision)
        except Exception:
            return WorkflowActionResult("failed", {"reason": "draft_commit_failed"})

    async def post_commit_capability_proof(self, invocation: WorkflowActionInvocation) -> WorkflowActionResult:
        try:
            revision = invocation.execution.committed_revision
            if not isinstance(revision, str) or not _COMMIT.fullmatch(revision):
                return WorkflowActionResult("failed", {"reason": "missing_committed_revision"})
            projection = await self._projection(invocation.execution.owner_id)
            if projection.commit_sha != revision:
                return WorkflowActionResult("blocked", {"reason": "committed_revision_not_projected"})
            draft_evidence = _latest_evidence(invocation, "create_repository_draft")
            specification = _specification(draft_evidence)
            capability_id = str(specification["capability_id"])
            index = await self._capabilities.get_capability_index(invocation.execution.owner_id)
            if index.get("repository_commit") != revision or not any(
                isinstance(item, Mapping) and item.get("id") == capability_id for item in index.get("capabilities", [])
            ):
                return WorkflowActionResult("blocked", {"reason": "capability_not_ready"})
            run = await self._capabilities.run_capability(invocation.execution.owner_id, capability_id)
            if run.repository_commit != revision:
                return WorkflowActionResult("blocked", {"reason": "capability_revision_drift"})
            expected = _mapping(_mapping(specification["acceptance"]).get("expected_result"))
            if run.result != dict(expected):
                return WorkflowActionResult("failed", {"reason": "acceptance_mismatch", "result": run.result})
            return WorkflowActionResult("success", {"capability_id": capability_id, "repository_commit": revision, "result": run.result})
        except Exception:
            return WorkflowActionResult("failed", {"reason": "post_commit_proof_failed"})

    async def _projection(self, owner_id: str):
        return await self._projections.synchronize(owner_id)


def validate_create_capability_admission(arguments: Mapping[str, object], base_revision: str) -> None:
    """Reject structurally invalid Create Capability inputs before execution persistence."""
    build_capability_specification(arguments, base_revision)


def build_capability_specification(arguments: Mapping[str, object], base_revision: str) -> dict[str, object]:
    """Build the same pure specification used by admission and the first workflow step."""
    capability_id = _identifier(arguments.get("capability_id", arguments.get("id")))
    title = _text(arguments.get("title"))
    description = _text(arguments.get("description"))
    input_contract = _contract(arguments.get("input_contract"))
    output_contract = _contract(arguments.get("output_contract"))
    effects = _effects(arguments.get("effects", []))
    acceptance = _acceptance(arguments)
    root = PurePosixPath("capabilities") / capability_id
    invocation_kind = "no_arguments" if not input_contract.get("properties") and not input_contract.get("required") else "object"
    return {
        "capability_id": capability_id, "title": title, "description": description, "version": "1.0",
        "base_revision": base_revision,
        "capability_path": str(root / "capability.json"), "executor_path": str(root / "executor.py"),
        "tests_path": str(root / "tests"), "catalog_path": "capabilities/capabilities-manifest.json",
        "runtime_binding": {
            "kind": "context_python_callable", "path": "/" + str(root / "executor.py"),
            "callable": capability_id.replace("-", "_"), "invocation": invocation_kind,
        },
        "input_contract": input_contract, "output_contract": output_contract, "effects": effects,
        "acceptance": acceptance, "test_expectations": {"focused_tests": True, "catalog_registration": True},
    }


def _result(invocation: WorkflowActionInvocation, reference: str) -> dict[str, object]:
    if not reference.startswith("result."):
        raise ValueError("invalid_evidence_reference")
    item = invocation.execution.step_results.get(reference.removeprefix("result."))
    if not isinstance(item, Mapping):
        raise ValueError("missing_referenced_evidence")
    return dict(_mapping(item.get("evidence")))


def _latest_evidence(invocation: WorkflowActionInvocation, action: str) -> dict[str, object]:
    step = next((step for step in invocation.plan.steps if step.action == action), None)
    if step is None:
        raise ValueError("missing_action_evidence")
    return _result(invocation, "result." + step.id)


def _specification(evidence: Mapping[str, object]) -> dict[str, object]:
    value = evidence.get("specification", evidence)
    specification = dict(_mapping(value))
    required = {"capability_id", "capability_path", "executor_path", "tests_path", "catalog_path", "runtime_binding", "acceptance"}
    if not required <= set(specification):
        raise ValueError("missing_capability_specification")
    return specification


def _allowed_paths(specification: Mapping[str, object]) -> list[str]:
    capability_path = str(specification["capability_path"])
    root = str(PurePosixPath(capability_path).parent)
    catalog = str(specification["catalog_path"])
    return [root, catalog]


def _allowed_change(path: str, specification: Mapping[str, object]) -> bool:
    root, catalog = _allowed_paths(specification)
    return path == catalog or path.startswith(root + "/")


def _verify_replacements(specification: Mapping[str, object], replacements: list[dict[str, str]]) -> None:
    expected_root = str(PurePosixPath(str(specification["capability_path"])).parent)
    catalog_path = str(specification["catalog_path"])
    allowed = {catalog_path}
    paths = {item["path"] for item in replacements}
    if any(not (path == catalog_path or path.startswith(expected_root + "/")) for path in paths):
        raise ValueError("unrelated_proposal_change")
    required = {str(specification["capability_path"]), str(specification["executor_path"]), catalog_path}
    if not required <= paths or not any(path.startswith(str(specification["tests_path"]) + "/") for path in paths):
        raise ValueError("incomplete_capability_proposal")
    definition = _json_replacement(replacements, str(specification["capability_path"]))
    if definition.get("content_type") != "mabel_capability" or definition.get("id") != specification["capability_id"]:
        raise ValueError("capability_definition_mismatch")
    if definition.get("runtime_binding") != specification["runtime_binding"]:
        raise ValueError("unsupported_runtime_binding")
    if definition.get("effects") != specification.get("effects"):
        raise ValueError("effects_mismatch")
    _contract(definition.get("input_contract")); output = _contract(definition.get("output_contract"))
    try:
        Draft202012Validator(output).validate(_mapping(_mapping(specification["acceptance"]).get("expected_result")))
    except Exception as error:
        raise ValueError("acceptance_output_mismatch") from error
    catalog = _json_replacement(replacements, catalog_path)
    entries = catalog.get("capabilities")
    target = next((entry for entry in entries if isinstance(entry, Mapping) and entry.get("id") == specification["capability_id"]), None) if isinstance(entries, list) else None
    if catalog.get("content_type") != "mabel_capability_catalog" or not isinstance(target, Mapping) or target.get("path") != "/" + str(specification["capability_path"]):
        raise ValueError("catalog_capability_mismatch")


def _replacements(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ValueError("missing_proposed_replacements")
    result: list[dict[str, str]] = []
    for item in value:
        record = _mapping(item)
        path, content = record.get("path"), record.get("content")
        if record.get("change", "replace") != "replace" or not isinstance(path, str) or not isinstance(content, str):
            raise ValueError("unsupported_proposed_replacement")
        result.append({"path": path.lstrip("/"), "content": content})
    if len({item["path"] for item in result}) != len(result):
        raise ValueError("duplicate_proposed_replacement")
    return result


def _json_replacement(replacements: list[dict[str, str]], path: str) -> dict[str, object]:
    item = next((item for item in replacements if item["path"] == path), None)
    if item is None:
        raise ValueError("missing_proposed_replacement")
    try:
        value = json.loads(item["content"])
    except json.JSONDecodeError as error:
        raise ValueError("invalid_proposed_json") from error
    return dict(_mapping(value))


def _gate_was_reached(invocation: WorkflowActionInvocation, gate_id: str) -> bool:
    step = next((step for step in invocation.plan.steps if step.action == "operator_gate" and step.parameters.get("gate_id") == gate_id), None)
    return step is not None and step.id in invocation.execution.step_results


def _acceptance(arguments: Mapping[str, object]) -> dict[str, object]:
    value = arguments.get("acceptance", {"expected_result": arguments.get("expected_result")})
    acceptance = dict(_mapping(value))
    expected = _mapping(acceptance.get("expected_result"))
    return {"expected_result": dict(expected)}


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not _CAPABILITY_ID.fullmatch(value):
        raise ValueError("invalid_capability_id")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("invalid_capability_specification")
    return value.strip()


def _contract(value: object) -> dict[str, object]:
    contract = dict(_mapping(value))
    if contract.get("type") != "object":
        raise ValueError("invalid_capability_contract")
    try:
        Draft202012Validator.check_schema(contract)
    except SchemaError as error:
        raise ValueError("invalid_capability_contract") from error
    return contract


def _effects(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ValueError("invalid_capability_effects")
    return [dict(_mapping(item)) for item in value]


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("invalid_structured_evidence")
    return value


def _strings(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("invalid_structured_evidence")
    return list(value)


def _safe_draft(value: Mapping[str, object]) -> dict[str, object]:
    keys = {"draft_id", "status", "files", "diff_truncated", "expires_at", "github_commit_sha"}
    return {key: value[key] for key in keys if key in value}
