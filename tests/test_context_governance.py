import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from mabel.context.discovery import ContextRepositoryError, MabelContextDiscovery
from mabel.context.execution import BoundedCapabilityRunner
from mabel.repository import LocalRepository
from mabel.services.capabilities import CapabilityService
from mabel.services.capability_controls import CapabilityControlService
from mabel.services.mail import MailDeliveryReceipt
from mabel.services.notifications import NotificationService
from mabel.state.capability_controls import SqliteCapabilityControlStore
from mabel.state.database import SqliteDatabase


CONTEXT_CAPABILITY_IDS = [
    "echo", "return_123456", "send_notification", "get_context_manifest", "get_context_file",
    "create_context_draft", "get_context_draft", "commit_context_draft", "discard_context_draft",
    "run_workflow",
]


def _context_copy(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[2] / "mabel-context"
    target = tmp_path / "context"
    shutil.copytree(source, target)
    return target


def _load(path: Path):
    return MabelContextDiscovery(LocalRepository(path)).load()


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _registry_v1_context(root: Path) -> None:
    schema = '{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object"}\n'
    files = {
        "manifest.json": '{"schema_version":1,"system":{"role":"user_operational_context"},"instructions":{"start":"instructions/start.md"},"schemas":{"manifest":"schemas/manifest.json"},"capabilities":{"manifest":"capabilities/manifest.json"},"workflows":{"manifest":"workflows/manifest.json"}}\n',
        "instructions/start.md": "# Context\n",
        "schemas/manifest.json": json.dumps({
            "format_version": "1.0", "content_type": "schema_registry", "content_types": [
                {"id": name, "title": name, "content_type": name, "current_version": "1.0", "versions": [{"version": "1.0", "schema_path": f"schemas/schemas/{name}.schema.json", "rules_path": f"schemas/rules/{name}.rules.md", "status": "active"}]}
                for name in ("schema_registry", "capability_manifest", "capability", "workflow_manifest", "workflow")
            ],
        }),
        "capabilities/manifest.json": '{"format_version":"1.0","schema_version":"1.0","content_type":"capability","entries":[{"id":"echo","path":"capabilities/echo/capability.json","schema_version":"1.0"}]}\n',
        "workflows/manifest.json": '{"format_version":"1.0","schema_version":"1.0","content_type":"workflow","entries":[]}\n',
        "capabilities/echo/capability.json": '{"schema_version":"1.0","id":"echo","title":"Echo","description":"diagnostic","version":"1.0","execution":{"kind":"python_callable","path":"capabilities/echo/executor.py","callable":"echo","invocation":"no_arguments"},"input_contract":{"type":"object"},"output_contract":{"type":"object"},"effects":[],"verification":{"tests_path":"capabilities/echo/tests","execution_proof":"required"}}\n',
        "capabilities/echo/executor.py": 'def echo():\n    return {"message": "hello world"}\n',
        "capabilities/echo/tests/test_echo.py": "def test_echo():\n    assert True\n",
    }
    for name in ("schema_registry", "capability_manifest", "capability", "workflow_manifest", "workflow"):
        files[f"schemas/schemas/{name}.schema.json"] = schema
        files[f"schemas/rules/{name}.rules.md"] = f"# {name}\n"
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content + ("\n" if not content.endswith("\n") else ""), encoding="utf-8")


def test_amie_context_discovers_markdown_workflows_and_capabilities(tmp_path: Path) -> None:
    context_root = _context_copy(tmp_path)
    discovery = MabelContextDiscovery(LocalRepository(context_root))

    context = discovery.load()

    assert context.manifest.generation == "amie"
    assert [item.id for item in context.capabilities] == CONTEXT_CAPABILITY_IDS
    assert [item.path.suffix for item in context.workflows] == [".md", ".md", ".md"]
    assert discovery.load_workflow("define_content_schema").procedure == ()
    assert discovery.load_capability("send_notification").runtime_binding == {
        "kind": "gateway_primitive", "primitive": "notification_delivery", "invocation": "object",
    }


def test_amie_workflow_entries_do_not_require_gateway_schema_stamps(tmp_path: Path) -> None:
    context_root = _context_copy(tmp_path)
    workflow_manifest = context_root / "workflows/manifest.json"
    raw = json.loads(workflow_manifest.read_text(encoding="utf-8"))
    for entries in raw.values():
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    entry.pop("schema_version", None)
    _write_json(workflow_manifest, raw)

    discovery = MabelContextDiscovery(LocalRepository(context_root))
    context = discovery.load()

    assert [item.id for item in context.workflows] == [
        "define_content_schema", "create_workflow", "create_capability",
    ]
    assert [item.schema_version for item in context.workflows] == [None, None, None]
    assert context.collection_errors is None
    assert discovery.load_workflow("create_workflow").summary.schema_version is None


def test_amie_workflow_schema_stamp_is_validated_when_present(tmp_path: Path) -> None:
    context_root = _context_copy(tmp_path)
    workflow_manifest = context_root / "workflows/manifest.json"
    raw = json.loads(workflow_manifest.read_text(encoding="utf-8"))
    raw["schemas"][0]["schema_version"] = "invalid"
    _write_json(workflow_manifest, raw)

    context = _load(context_root)

    assert context.workflows == ()
    assert context.collection_errors == {
        "workflows": "workflow schema_version must use major.minor version syntax."
    }


def test_registry_v1_context_remains_supported_during_rollout(tmp_path: Path) -> None:
    context_root = tmp_path / "registry-v1"
    _registry_v1_context(context_root)

    context = _load(context_root)

    assert context.manifest.generation == "registry_v1"
    assert [item.id for item in context.capabilities] == ["echo"]


async def test_amie_context_executes_echo_and_a_generic_gateway_primitive(tmp_path: Path) -> None:
    context_root = _context_copy(tmp_path)
    discovery = MabelContextDiscovery(LocalRepository(context_root))
    context = discovery.load()
    projection = SimpleNamespace(context=context, discovery=discovery, root=context_root, commit_sha="a" * 40)

    class StaticProjections:
        async def synchronize(self, owner_id: str):
            assert owner_id == "owner"
            return projection

    class Delivery:
        async def send(self, message):
            return MailDeliveryReceipt(provider="test", external_id="delivery-1")

    projections = StaticProjections()
    notifications = NotificationService(Delivery(), "owner@example.test")
    database = SqliteDatabase(tmp_path / "state.db")
    database.initialize()
    controls = CapabilityControlService(projections, SqliteCapabilityControlStore(database), notifications)
    service = CapabilityService(projections, BoundedCapabilityRunner(), notifications, controls=controls)

    index = await service.get_capability_index("owner")
    entries = {entry["id"]: entry for entry in index["capabilities"]}
    notification_definition = discovery.load_capability("send_notification")
    assert entries["echo"]["input_contract"] == discovery.load_capability("echo").input_contract
    assert entries["echo"]["input_contract"] == {
        "type": "object", "properties": {}, "required": [], "additionalProperties": False,
    }
    assert entries["return_123456"]["input_contract"] == discovery.load_capability(
        "return_123456"
    ).input_contract
    assert entries["return_123456"]["input_contract"] == {
        "type": "object", "properties": {}, "required": [], "additionalProperties": False,
    }
    assert entries["send_notification"]["input_contract"] == notification_definition.input_contract
    assert entries["send_notification"]["input_contract"]["required"] == ["message"]
    assert entries["send_notification"]["input_contract"]["properties"]["severity"] == {
        "enum": ["info", "success", "warning", "error"],
    }
    assert entries["send_notification"]["input_contract"]["additionalProperties"] is False

    # A generic caller derives the only required argument name from discovery;
    # it does not know send_notification's vocabulary beforehand.
    discovered_arguments = {
        field: "MABEL send_notification verification from AMIE"
        for field in entries["send_notification"]["input_contract"]["required"]
    }

    echo = await service.run_capability("owner", "echo")
    return_123456 = await service.run_capability("owner", "return_123456")
    notification = await service.run_capability(
        "owner",
        "send_notification",
        discovered_arguments,
    )

    assert echo.result == {"message": "hello world"}
    assert return_123456.result == {"value": "123456"}
    assert notification.result == {
        "delivered": True, "provider": "test", "external_id": "delivery-1", "retryable": False,
    }


async def test_amie_context_capability_uses_declared_primitive_operation_not_capability_id(tmp_path: Path) -> None:
    context_root = _context_copy(tmp_path)
    discovery = MabelContextDiscovery(LocalRepository(context_root))
    context = discovery.load()
    projection = SimpleNamespace(context=context, discovery=discovery, root=context_root, commit_sha="b" * 40)

    class StaticProjections:
        async def synchronize(self, owner_id: str):
            assert owner_id == "owner"
            return projection

    class ContextBridge:
        def __init__(self) -> None:
            self.calls = []

        async def run(self, owner_id, operation, arguments):
            self.calls.append((owner_id, operation, arguments))
            return {"ok": True, "path": arguments["path"], "content": "body", "sha": "abc"}

        async def readiness_error_code(self, owner_id):
            return None

    bridge = ContextBridge()
    projections = StaticProjections()
    database = SqliteDatabase(tmp_path / "state.db")
    database.initialize()
    controls = CapabilityControlService(projections, SqliteCapabilityControlStore(database), amie_context=bridge)
    service = CapabilityService(projections, BoundedCapabilityRunner(), amie_context=bridge, controls=controls)

    result = await service.run_capability("owner", "get_context_file", {"path": "/instructions/start.md"})

    assert result.result == {"ok": True, "path": "/instructions/start.md", "content": "body", "sha": "abc"}
    assert bridge.calls == [("owner", "get_file", {"path": "/instructions/start.md"})]


def test_invalid_workflow_collection_is_isolated_from_capabilities(tmp_path: Path) -> None:
    context_root = _context_copy(tmp_path)
    workflow_manifest = context_root / "workflows/manifest.json"
    raw = json.loads(workflow_manifest.read_text(encoding="utf-8"))
    raw["schemas"][0]["path"] = "/workflows/schemas/define-content-schema.json"
    _write_json(workflow_manifest, raw)

    context = _load(context_root)

    assert [item.id for item in context.capabilities] == CONTEXT_CAPABILITY_IDS
    assert context.workflows == ()
    assert context.collection_errors and "workflows" in context.collection_errors


def test_invalid_amie_schema_pair_rejects_context_before_capability_discovery(tmp_path: Path) -> None:
    context_root = _context_copy(tmp_path)
    schema_path = context_root / "schemas/schemas/mabel-capability.schema.json"
    raw = json.loads(schema_path.read_text(encoding="utf-8"))
    raw["rules_path"] = "/schemas/rules/not-the-paired-rules.md"
    _write_json(schema_path, raw)

    with pytest.raises(ContextRepositoryError, match="rules_path does not match"):
        _load(context_root)


def test_invalid_capability_catalog_remains_fatal(tmp_path: Path) -> None:
    context_root = _context_copy(tmp_path)
    catalog_path = context_root / "capabilities/capabilities-manifest.json"
    raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    raw["capabilities"][0]["path"] = "/capabilities/not-echo/capability.json"
    _write_json(catalog_path, raw)

    with pytest.raises(ContextRepositoryError, match="canonical capability.json path"):
        _load(context_root)


def test_capability_contracts_remain_json_schema_machine_contracts(tmp_path: Path) -> None:
    context_root = _context_copy(tmp_path)
    capability_path = context_root / "capabilities/send_notification/capability.json"
    raw = json.loads(capability_path.read_text(encoding="utf-8"))
    raw["input_contract"] = {"type": "not-a-real-json-schema-type"}
    _write_json(capability_path, raw)

    with pytest.raises(ContextRepositoryError, match="not valid JSON Schema"):
        MabelContextDiscovery(LocalRepository(context_root)).load_capability("send_notification")
