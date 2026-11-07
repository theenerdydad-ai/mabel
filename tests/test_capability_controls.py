from dataclasses import dataclass
from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from mabel.app import create_app
from mabel.config import Settings
from mabel.context.models import CapabilityDefinition, CapabilitySummary, ContextManifest, LoadedContext
from mabel.services.capabilities import CapabilityExecutionFailureError, CapabilityService
from mabel.context.projection import ContextProjectionError
from mabel.services.capabilities import CapabilityServiceError
from mabel.services.capability_controls import CapabilityControlService, CapabilityControlServiceError
from mabel.services.notifications import NotificationService
from mabel.state.capability_controls import SqliteCapabilityControlStore
from mabel.state.database import SqliteDatabase


class StaticDiscovery:
    def __init__(self, definitions, failures=None):
        self._definitions = {definition.summary.id: definition for definition in definitions}
        self._failures = failures or {}

    def load_capability(self, capability_id):
        if capability_id in self._failures:
            raise self._failures[capability_id]
        return self._definitions[capability_id]


@dataclass
class StaticProjections:
    projection: object

    async def synchronize(self, owner_id):
        return self.projection


class FakeRunner:
    def run(self, projection, capability, arguments):
        return {"message": "hello world"}


class FakeMailDelivery:
    async def send(self, message):
        raise AssertionError("A readiness check must not send mail.")


class MutableMailDelivery(FakeMailDelivery):
    def __init__(self, configured=True) -> None:
        self.configured = configured

    def is_configured(self) -> bool:
        return self.configured


def _definitions():
    echo = CapabilityDefinition(
        CapabilitySummary("echo", "Echo", PurePosixPath("capabilities/echo/capability.json"), "1.0"),
        PurePosixPath("capabilities/echo/executor.py"), "echo", None, {},
        input_contract={
            "type": "object", "properties": {}, "required": [], "additionalProperties": False,
        }, output_contract={"type": "object"},
        execution_kind="context_python_callable", invocation="no_arguments",
        runtime_binding={"kind": "context_python_callable"},
    )
    notification = CapabilityDefinition(
        CapabilitySummary("send_notification", "Send Notification", PurePosixPath("capabilities/send_notification/capability.json"), "1.0"),
        None, None, None, {}, input_contract={
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "severity": {"enum": ["info", "success", "warning", "error"]},
                "metadata": {
                    "type": "object",
                    "properties": {"run_id": {"type": "string"}},
                    "required": ["run_id"],
                    "additionalProperties": False,
                },
            },
            "required": ["message"],
            "additionalProperties": False,
        }, output_contract={"type": "object"},
        execution_kind="gateway_primitive", invocation="object",
        runtime_binding={"kind": "gateway_primitive", "primitive": "notification_delivery"},
    )
    return echo, notification


def _projection(definitions=None, failures=None):
    definitions = _definitions() if definitions is None else definitions
    summaries = tuple(definition.summary for definition in definitions)
    if failures:
        summaries += tuple(failures)
    context = LoadedContext(
        ContextManifest(PurePosixPath("instructions/start.md"), PurePosixPath("schemas/manifest.json"), PurePosixPath("capabilities/manifest.json"), {}),
        summaries,
    )
    return SimpleNamespace(
        context=context, discovery=StaticDiscovery(definitions, {summary.id: error for summary, error in (failures or {}).items()}), commit_sha="a" * 40, root=PurePosixPath(".")
    )


def _services(tmp_path, recipient="owner@example.test", delivery=None, projection=None, amie_context=None):
    database = SqliteDatabase(tmp_path / "state.db")
    database.initialize()
    projections = StaticProjections(projection or _projection())
    notifications = NotificationService(delivery or FakeMailDelivery(), recipient)
    controls = CapabilityControlService(
        projections, SqliteCapabilityControlStore(database), notifications, amie_context
    )
    capabilities = CapabilityService(projections, FakeRunner(), notifications, amie_context, controls)
    return database, controls, capabilities


async def test_control_state_is_owner_scoped_and_missing_rows_default_to_enabled(tmp_path) -> None:
    database, controls, _ = _services(tmp_path)
    projection = _projection()
    echo = projection.discovery.load_capability("echo")

    assert (await controls.for_projection("owner-a", projection, echo)).enabled is True
    controls._controls.set_enabled("owner-a", "echo", False, echo_time())

    assert (await controls.for_projection("owner-a", projection, echo)).status == "inactive"
    assert (await controls.for_projection("owner-b", projection, echo)).status == "active"
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM capability_controls").fetchone()[0] == 1


async def test_management_api_lists_all_declared_capabilities_and_mutates_only_the_current_owner(tmp_path) -> None:
    database, controls, capabilities = _services(tmp_path)
    app = create_app(
        capability_service=capabilities,
        capability_control_service=controls,
        settings=Settings(state_database_path=database.path, development_owner_id="test-owner"),
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            initial = await client.get("/api/portal/capabilities")
            disabled = await client.patch("/api/portal/capabilities/echo", json={"enabled": False})
            listed = await client.get("/api/portal/capabilities")
            checked = await client.post("/api/portal/capabilities/send_notification/validate")
            unknown = await client.patch("/api/portal/capabilities/not-declared", json={"enabled": False})

    assert initial.status_code == 200
    assert [item["id"] for item in initial.json()["capabilities"]] == ["echo", "send_notification"]
    assert disabled.json()["status"] == "inactive"
    assert [item["id"] for item in listed.json()["capabilities"]] == ["echo", "send_notification"]
    assert next(item for item in listed.json()["capabilities"] if item["id"] == "echo")["enabled"] is False
    assert checked.json()["status"] == "active"
    assert checked.json()["validation"]["state"] == "passed"
    assert unknown.status_code == 404
    assert "secret" not in listed.text.lower()


async def test_index_and_execution_recheck_owner_scoped_eligibility(tmp_path) -> None:
    _, controls, capabilities = _services(tmp_path)
    controls._controls.set_enabled("owner-a", "echo", False, echo_time())

    owner_a_index = await capabilities.get_capability_index("owner-a")
    owner_b_index = await capabilities.get_capability_index("owner-b")

    assert [item["id"] for item in owner_a_index["capabilities"]] == ["send_notification"]
    assert {item["id"] for item in owner_b_index["capabilities"]} == {"echo", "send_notification"}
    with pytest.raises(CapabilityExecutionFailureError):
        await capabilities.run_capability("owner-a", "echo")
    assert (await capabilities.run_capability("owner-b", "echo")).result == {"message": "hello world"}


async def test_live_index_projects_the_exact_validated_input_contract(tmp_path) -> None:
    _, _, capabilities = _services(tmp_path)
    projection = capabilities._projections.projection

    index = await capabilities.get_capability_index("owner-a")
    entries = {entry["id"]: entry for entry in index["capabilities"]}
    echo = projection.discovery.load_capability("echo")
    notification = projection.discovery.load_capability("send_notification")

    assert entries["echo"]["input_contract"] == echo.input_contract
    assert entries["echo"]["input_contract"] == {
        "type": "object", "properties": {}, "required": [], "additionalProperties": False,
    }
    assert entries["send_notification"]["input_contract"] == notification.input_contract
    assert entries["send_notification"]["input_contract"]["required"] == ["message"]
    assert entries["send_notification"]["input_contract"]["properties"]["severity"] == {
        "enum": ["info", "success", "warning", "error"],
    }
    assert entries["send_notification"]["input_contract"]["properties"]["metadata"] == {
        "type": "object",
        "properties": {"run_id": {"type": "string"}},
        "required": ["run_id"],
        "additionalProperties": False,
    }
    assert entries["send_notification"]["input_contract"]["additionalProperties"] is False

    entries["send_notification"]["input_contract"]["required"].append("not_authoritative")
    assert notification.input_contract["required"] == ["message"]


async def test_notification_readiness_hides_index_and_blocks_execution_without_sending(tmp_path) -> None:
    _, controls, capabilities = _services(tmp_path, recipient=None)

    management = await controls.list("owner-a")
    index = await capabilities.get_capability_index("owner-a")

    notification = next(item for item in management["capabilities"] if item["id"] == "send_notification")
    assert notification["status"] == "needs_setup"
    assert notification["reason_code"] == "notification_not_configured"
    assert [item["id"] for item in index["capabilities"]] == ["echo"]
    with pytest.raises(CapabilityExecutionFailureError):
        await capabilities.run_capability("owner-a", "send_notification", {"message": "not sent"})


async def test_malformed_artifact_is_isolated_from_management_discovery_and_execution(tmp_path) -> None:
    malformed = CapabilitySummary(
        "broken", "Broken", PurePosixPath("capabilities/broken/capability.json"), "1.0"
    )
    projection = _projection(failures={malformed: ValueError("token=must-not-leak")})
    _, controls, capabilities = _services(tmp_path, projection=projection)

    management = await controls.list("owner-a")
    index = await capabilities.get_capability_index("owner-a")

    broken = next(item for item in management["capabilities"] if item["id"] == "broken")
    assert broken["status"] == "error"
    assert broken["reason_code"] == "invalid_capability_artifact"
    assert "must-not-leak" not in str(broken)
    assert [item["id"] for item in index["capabilities"]] == ["echo", "send_notification"]
    with pytest.raises(CapabilityExecutionFailureError):
        await capabilities.run_capability("owner-a", "broken")


async def test_context_readiness_is_rechecked_before_execution(tmp_path) -> None:
    context = CapabilityDefinition(
        CapabilitySummary("get_context_manifest", "Get Context Manifest", PurePosixPath("capabilities/get_context_manifest/capability.json"), "1.0"),
        None, None, None, {}, input_contract={"type": "object"}, output_contract={"type": "object"},
        execution_kind="gateway_primitive", invocation="object",
        runtime_binding={"kind": "gateway_primitive", "primitive": "amie_context_repository", "operation": "get_manifest"},
    )

    class ContextBridge:
        ready = True

        async def readiness_error_code(self, owner_id):
            return None if self.ready else "amie_connection_not_configured"

        async def run(self, owner_id, operation, arguments):
            raise AssertionError("Ineligible context capability must not dispatch.")

    bridge = ContextBridge()
    projection = _projection(definitions=(context,))
    _, controls, capabilities = _services(tmp_path, projection=projection, amie_context=bridge)

    assert [item["id"] for item in (await capabilities.get_capability_index("owner-a"))["capabilities"]] == ["get_context_manifest"]
    bridge.ready = False
    assert (await controls.list("owner-a"))["capabilities"][0]["status"] == "needs_setup"
    assert (await capabilities.get_capability_index("owner-a"))["capabilities"] == []
    with pytest.raises(CapabilityExecutionFailureError):
        await capabilities.run_capability("owner-a", "get_context_manifest", {})


async def test_unsupported_runtime_is_bounded_and_validation_is_not_execution_authority(tmp_path) -> None:
    unsupported = CapabilityDefinition(
        CapabilitySummary("unsupported", "Unsupported", PurePosixPath("capabilities/unsupported/capability.json"), "1.0"),
        None, None, None, {}, input_contract={"type": "object"}, output_contract={"type": "object"},
        execution_kind="gateway_primitive", invocation="object",
        runtime_binding={"kind": "gateway_primitive", "primitive": "unsupported"},
    )
    projection = _projection(definitions=(unsupported,))
    _, controls, capabilities = _services(tmp_path, projection=projection)

    management = await controls.list("owner-a")
    assert management["capabilities"][0]["status"] == "error"
    assert management["capabilities"][0]["reason_code"] == "unsupported_runtime_binding"
    controls._controls.record_validation("owner-a", "unsupported", "passed", None, echo_time())
    assert (await capabilities.get_capability_index("owner-a"))["capabilities"] == []
    with pytest.raises(CapabilityExecutionFailureError):
        await capabilities.run_capability("owner-a", "unsupported", {})


async def test_projection_failure_remains_fatal(tmp_path) -> None:
    class FailingProjections:
        async def synchronize(self, owner_id):
            raise ContextProjectionError("root manifest failed")

    database = SqliteDatabase(tmp_path / "state.db")
    database.initialize()
    projections = FailingProjections()
    controls = CapabilityControlService(projections, SqliteCapabilityControlStore(database))
    capabilities = CapabilityService(projections, FakeRunner(), controls=controls)

    with pytest.raises(CapabilityControlServiceError):
        await controls.list("owner-a")
    with pytest.raises(CapabilityServiceError):
        await capabilities.get_capability_index("owner-a")


def test_capability_service_requires_controls(tmp_path) -> None:
    with pytest.raises(ValueError, match="controls"):
        CapabilityService(StaticProjections(_projection()), FakeRunner())


def echo_time():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)
