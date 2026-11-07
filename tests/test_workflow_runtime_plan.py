import json
from pathlib import Path

import pytest

from mabel.context.discovery import ContextRepositoryError, MabelContextDiscovery
from mabel.context.workflow_plan import WorkflowRuntimePlanError, parse_workflow_runtime_plan
from mabel.repository import LocalRepository


def _workflow_text() -> str:
    return (
        Path(__file__).resolve().parents[2]
        / "mabel-context/workflows/capabilities/create-capability.md"
    ).read_text(encoding="utf-8")


def _plan_text(text: str, mutate) -> str:
    opening = "```mabel-runtime-plan\n"
    start = text.index(opening) + len(opening)
    end = text.index("\n```", start)
    plan = json.loads(text[start:end])
    mutate(plan)
    return text[:start] + json.dumps(plan, indent=2) + text[end:]


def test_create_capability_runtime_plan_is_document_owned_and_deterministic() -> None:
    plan = parse_workflow_runtime_plan(_workflow_text())

    assert plan is not None
    assert plan.initial_step_id == "prepare_capability_specification"
    assert [step.action for step in plan.steps] == [
        "prepare_capability_specification",
        "invoke_governed_agent",
        "evaluate_development_evidence",
        "operator_gate",
        "verify_capability_proposal",
        "create_repository_draft",
        "operator_gate",
        "commit_approved_draft",
        "post_commit_capability_proof",
    ]
    assert plan.steps[1].parameters["agent"] == {"agent_class": "code_development"}
    assert plan.steps[6].transitions["approved"] == "commit_approved_draft"
    assert plan.state_contract["on_revision_drift"] == "blocked"


def test_create_capability_runtime_plan_loads_from_authoritative_context() -> None:
    root = Path(__file__).resolve().parents[2] / "mabel-context"
    workflow = MabelContextDiscovery(LocalRepository(root)).load_workflow("create_capability")

    assert workflow.runtime_plan is not None
    assert workflow.runtime_plan.gates[1].id == "exact_diff_approval"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda plan: plan["steps"][0].__setitem__("id", "not-a-stable-id"), "lowercase identifier"),
        (lambda plan: plan["steps"][0]["transitions"].__setitem__("success", "missing_step"), "does not name a step"),
        (lambda plan: plan["steps"][1].__setitem__("id", "prepare_capability_specification"), "step IDs must be unique"),
        (lambda plan: plan["steps"][0].__setitem__("action", "shell_command"), "action is unsupported"),
        (lambda plan: plan["steps"][0]["transitions"].pop("resource_limited"), "must declare every bounded action outcome"),
    ],
)
def test_runtime_plan_rejects_invalid_step_contracts(mutate, message: str) -> None:
    with pytest.raises(WorkflowRuntimePlanError, match=message):
        parse_workflow_runtime_plan(_plan_text(_workflow_text(), mutate))


@pytest.mark.parametrize("field", ["provider", "model", "credential"])
def test_runtime_plan_rejects_provider_details_in_agent_reference(field: str) -> None:
    def mutate(plan):
        plan["steps"][1]["parameters"]["agent"][field] = "not_governed_here"

    with pytest.raises(WorkflowRuntimePlanError, match="unsupported or missing fields"):
        parse_workflow_runtime_plan(_plan_text(_workflow_text(), mutate))


def test_runtime_plan_requires_exact_diff_approval_before_commit() -> None:
    def mutate(plan):
        plan["steps"][7]["parameters"]["gate_id"] = "development_result_review"

    with pytest.raises(WorkflowRuntimePlanError, match="commit must follow its exact-diff approval gate"):
        parse_workflow_runtime_plan(_plan_text(_workflow_text(), mutate))


def test_runtime_plan_rejects_duplicate_gates_and_automatic_commit_metadata() -> None:
    def duplicate_gate(plan):
        plan["gates"][1]["id"] = "development_result_review"

    with pytest.raises(WorkflowRuntimePlanError, match="gate IDs must be unique"):
        parse_workflow_runtime_plan(_plan_text(_workflow_text(), duplicate_gate))

    def automatic_commit(plan):
        plan["steps"][7]["parameters"]["automatic"] = True

    with pytest.raises(WorkflowRuntimePlanError, match="parameters do not match"):
        parse_workflow_runtime_plan(_plan_text(_workflow_text(), automatic_commit))


def test_runtime_plan_rejects_gate_with_a_different_approved_transition() -> None:
    def mutate(plan):
        plan["steps"][3]["transitions"]["approved"] = "create_repository_draft"

    with pytest.raises(WorkflowRuntimePlanError, match="approval must use its declared continuation"):
        parse_workflow_runtime_plan(_plan_text(_workflow_text(), mutate))


def test_descriptive_workflows_remain_valid_without_a_runtime_plan() -> None:
    root = Path(__file__).resolve().parents[2] / "mabel-context"
    workflow = MabelContextDiscovery(LocalRepository(root)).load_workflow("create_workflow")

    assert workflow.runtime_plan is None


def test_context_loading_rejects_a_malformed_document_owned_runtime_plan(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[2] / "mabel-context"
    root = tmp_path / "context"
    import shutil
    shutil.copytree(source, root)
    workflow_path = root / "workflows/capabilities/create-capability.md"
    workflow_path.write_text(
        _plan_text(_workflow_text(), lambda plan: plan["steps"][0].__setitem__("action", "python")),
        encoding="utf-8",
    )

    with pytest.raises(ContextRepositoryError, match="Workflow Runtime Plan is invalid"):
        MabelContextDiscovery(LocalRepository(root)).load_workflow("create_capability")
