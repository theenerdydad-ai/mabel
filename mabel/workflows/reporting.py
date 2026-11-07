"""Safe, deny-by-default reporting projections for workflow executions."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from uuid import UUID

from mabel.workflows.models import WorkflowExecution


_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,127}")
_SAFE_VERSION = re.compile(r"[A-Za-z0-9._-]{1,64}")
_SAFE_PROPOSAL = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_SHA = re.compile(r"[0-9a-f]{40}")
_TERMINAL = frozenset({"completed", "failed", "blocked", "resource_limited"})
_ATTENTION = frozenset({"stopped", "blocked", "failed", "resource_limited"})
_PROVIDERS = {"codex_cli": "codex"}
# Each persisted action must explicitly opt into the small evidence projection
# it is allowed to publish. New workflow actions therefore remain visible as
# completed steps while their evidence stays private until reviewed here.
_EVIDENCE_FIELDS_BY_ACTION = {
    "prepare_capability_specification": frozenset({"reason_code"}),
    "invoke_governed_agent": frozenset({"reason_code", "changed_files", "validation"}),
    "evaluate_development_evidence": frozenset({"reason_code", "accepted"}),
    "operator_gate": frozenset({"reason_code", "awaiting_operator"}),
    "verify_capability_proposal": frozenset({"reason_code", "verified"}),
    "create_repository_draft": frozenset({"reason_code"}),
    "commit_approved_draft": frozenset({"reason_code"}),
    "post_commit_capability_proof": frozenset({"reason_code"}),
}


def serialize_workflow_execution_list_item(execution: WorkflowExecution) -> dict[str, object]:
    """Return the small, safe projection needed by an operations list row."""
    return {
        "execution_id": _execution_id(execution.execution_id),
        "workflow": _workflow(execution),
        "status": _required_identifier(execution.status),
        "created_at": _timestamp(execution.created_at),
        "updated_at": _timestamp(execution.updated_at),
        "next_step_id": _identifier_or_none(execution.next_step_id),
        "active_gate_id": _identifier_or_none(execution.active_gate_id),
        "requires_attention": execution.status in _ATTENTION,
        "failure_code": _identifier_or_none(execution.error_code),
    }


def serialize_workflow_execution_detail(execution: WorkflowExecution) -> dict[str, object]:
    """Return only reviewed, bounded state; never expose raw execution evidence."""
    next_step_id = _identifier_or_none(execution.next_step_id)
    terminal_status = execution.status if execution.status in _TERMINAL and next_step_id is None else None
    return {
        "execution_id": _execution_id(execution.execution_id),
        "workflow": _workflow(execution),
        "status": _required_identifier(execution.status),
        "created_at": _timestamp(execution.created_at),
        "updated_at": _timestamp(execution.updated_at),
        "base_revision": _sha_or_none(execution.base_revision),
        "next_step_id": next_step_id,
        "active_gate_id": _identifier_or_none(execution.active_gate_id),
        "latest_gate_decision": _decision_or_none(execution.gate_decision),
        "proposal_id": _proposal_or_none(execution.proposal_id),
        "committed_revision": _sha_or_none(execution.committed_revision),
        "failure": {"code": _identifier_or_none(execution.error_code)} if execution.error_code else None,
        "progression": {
            "completed_steps": _completed_steps(execution.step_results),
            "next_step_id": next_step_id,
            "terminal_status": terminal_status,
        },
    }


def _workflow(execution: WorkflowExecution) -> dict[str, str]:
    return {
        "id": _required_identifier(execution.workflow_id),
        "path": _workflow_path(execution.workflow_path),
        "version": _required_version(execution.workflow_version),
    }


def _completed_steps(step_results: Mapping[str, object]) -> list[dict[str, object]]:
    summaries: list[dict[str, object]] = []
    # Persisted step results are a map, not an ordered event stream. Sorting
    # yields stable presentation without implying execution chronology.
    for step_id in sorted(step_results):
        result = step_results[step_id]
        if not isinstance(result, Mapping):
            continue
        outcome = result.get("outcome")
        if not isinstance(outcome, str) or not _IDENTIFIER.fullmatch(outcome):
            continue
        summary: dict[str, object] = {
            "step_id": _required_identifier(step_id),
            "outcome": outcome,
        }
        evidence = _safe_evidence(step_id, result.get("evidence"))
        if evidence:
            summary["evidence"] = evidence
        summaries.append(summary)
    return summaries


def _safe_evidence(step_id: str, value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    allowed = _EVIDENCE_FIELDS_BY_ACTION.get(step_id)
    if allowed is None:
        return {}
    result: dict[str, object] = {}
    reason = value.get("reason_code", value.get("reason"))
    if "reason_code" in allowed and isinstance(reason, str) and _IDENTIFIER.fullmatch(reason):
        result["reason_code"] = reason
    for key in ("accepted", "verified", "awaiting_operator"):
        if key in allowed and isinstance(value.get(key), bool):
            result[key] = value[key]
    if "changed_files" in allowed:
        changed_files = _changed_files(value.get("changed_files"))
        changes = value.get("changes")
        if not changed_files and isinstance(changes, Mapping):
            changed_files = _changed_files(changes.get("changed_files"))
        if changed_files:
            result["changed_files"] = changed_files
    validation = value.get("validation")
    if "validation" in allowed and isinstance(validation, Mapping):
        safe_validation = _validation(validation)
        if safe_validation:
            result["validation"] = safe_validation
    return result


def _changed_files(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    paths: list[str] = []
    for raw in value[:64]:
        if not isinstance(raw, str) or len(raw) > 256 or raw.startswith("/"):
            continue
        path = PurePosixPath(raw)
        if not raw or ".." in path.parts or str(path) in {"", "."}:
            continue
        paths.append(str(path))
    return paths


def _validation(value: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    provider = value.get("provider")
    if isinstance(provider, str) and provider in _PROVIDERS:
        result["provider"] = _PROVIDERS[provider]
    elapsed_ms = value.get("elapsed_ms")
    if isinstance(elapsed_ms, int) and not isinstance(elapsed_ms, bool) and 0 <= elapsed_ms <= 1_200_000:
        result["elapsed_ms"] = elapsed_ms
    exit_code = value.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool) and -128 <= exit_code <= 255:
        result["exit_code"] = exit_code
    return result


def _required_identifier(value: object) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        return "unknown"
    return value


def _execution_id(value: object) -> str:
    if not isinstance(value, str):
        return "unknown"
    try:
        return str(UUID(value))
    except ValueError:
        return "unknown"


def _identifier_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else None


def _required_version(value: object) -> str:
    return value if isinstance(value, str) and _SAFE_VERSION.fullmatch(value) else "unknown"


def _workflow_path(value: object) -> str:
    if not isinstance(value, str) or len(value) > 256 or value.startswith("/"):
        return "unknown"
    path = PurePosixPath(value)
    return str(path) if value and ".." not in path.parts and str(path) not in {"", "."} else "unknown"


def _sha_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and _SHA.fullmatch(value) else None


def _proposal_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and _SAFE_PROPOSAL.fullmatch(value) else None


def _decision_or_none(value: object) -> str | None:
    return value if value in {"approved", "declined"} else None


def _timestamp(value: object) -> str:
    return value.isoformat(timespec="microseconds") if hasattr(value, "isoformat") else "unknown"
