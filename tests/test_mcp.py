from pathlib import Path

from fastmcp import Client
from fastmcp.exceptions import ToolError
import pytest

from mabel.services.api_tokens import ApiTokenService
from mabel.mcp import create_mcp_server
from mabel.repository import LocalRepository
from mabel.services.configuration import ConfigurationService
from mabel.services.health import HealthService
from mabel.services.capabilities import CapabilityArgumentsError, CapabilityServiceError
from mabel.state.api_tokens import SqliteApiTokenStore
from mabel.state.database import SqliteDatabase


class FakeCapabilities:
    async def get_manifest(self, owner_id):
        return {"repository_commit": "a" * 40}

    async def get_capability_index(self, owner_id):
        return {"capabilities": [{"id": "echo"}]}

    async def run_capability(self, owner_id, capability_id, arguments=None):
        return FakeRunResult()


class FakeRunResult:
    def as_dict(self):
        return {
            "ok": True,
            "capability_id": "echo",
            "repository_commit": "a" * 40,
            "result": {"message": "hello world"},
        }


class FailingCapabilities(FakeCapabilities):
    async def get_capability_index(self, owner_id):
        raise CapabilityServiceError()

    async def run_capability(self, owner_id, capability_id, arguments=None):
        raise CapabilityArgumentsError()


class FakeContextBindings:
    def candidate(self, owner_id):
        class Candidate:
            def as_dict(self):
                return {
                    "ok": True,
                    "mabel_owner_id": owner_id,
                    "context_repository": {
                        "id": "00000000-0000-0000-0000-000000000002",
                        "provider": "github",
                        "provider_repository_id": "123",
                        "canonical_locator": "owner/context",
                        "status": "connected",
                        "enabled": True,
                    },
                }
        return Candidate()


def _api_tokens(tmp_path: Path) -> ApiTokenService:
    database = SqliteDatabase(tmp_path / "state.db")
    database.initialize()
    return ApiTokenService(SqliteApiTokenStore(database))


async def test_get_health_uses_the_shared_health_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration = ConfigurationService(LocalRepository(tmp_path))
    api_tokens = _api_tokens(tmp_path)
    issued = api_tokens.generate_or_regenerate("owner-a")
    monkeypatch.setattr(
        "mabel.mcp.get_http_headers",
        lambda **_: {"authorization": f"Bearer {issued.token}"},
    )
    mcp = create_mcp_server(HealthService(), configuration, api_tokens, FakeCapabilities())

    async with Client(mcp) as client:
        result = await client.call_tool("get_health", {})

    assert result.data == {"ok": True, "service": "mabel"}


async def test_get_mabel_configuration_loads_repository_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "instructions").mkdir()
    (tmp_path / "manifest.json").write_text(
        """{
  "schema_version": 1,
  "system": {"name": "MABEL", "expansion": "Mundane Architecture, Boringly Effective Logic", "role": "operational_runtime"},
  "instructions": {"start": "instructions/start.md"},
  "schemas": {"path": "schemas", "manifest": "schemas/manifest.json"}
}""",
        encoding="utf-8",
    )
    (tmp_path / "instructions" / "start.md").write_text("# Test instructions\n", encoding="utf-8")
    configuration = ConfigurationService(LocalRepository(tmp_path))
    api_tokens = _api_tokens(tmp_path)
    issued = api_tokens.generate_or_regenerate("owner-a")
    monkeypatch.setattr(
        "mabel.mcp.get_http_headers",
        lambda **_: {"authorization": f"Bearer {issued.token}"},
    )
    mcp = create_mcp_server(HealthService(), configuration, api_tokens, FakeCapabilities())

    async with Client(mcp) as client:
        result = await client.call_tool("get_mabel_configuration", {})

    assert result.data == {
        "manifest": {
            "schema_version": 1,
            "system": {
                "name": "MABEL",
                "expansion": "Mundane Architecture, Boringly Effective Logic",
                "role": "operational_runtime",
            },
            "instructions": {"start": "instructions/start.md"},
            "schemas": {"path": "schemas", "manifest": "schemas/manifest.json"},
        },
        "operating_instructions": {
            "start": {"path": "instructions/start.md", "content": "# Test instructions\n"}
        },
    }


async def test_mcp_requires_bearer_and_exposes_only_authenticated_identity(tmp_path: Path) -> None:
    configuration = ConfigurationService(LocalRepository(tmp_path))
    api_tokens = _api_tokens(tmp_path)
    mcp = create_mcp_server(HealthService(), configuration, api_tokens, FakeCapabilities())

    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="Authorization"):
            await client.call_tool("get_identity", {})



async def test_get_identity_uses_the_shared_bearer_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration = ConfigurationService(LocalRepository(tmp_path))
    api_tokens = _api_tokens(tmp_path)
    issued = api_tokens.generate_or_regenerate("owner-a")
    monkeypatch.setattr(
        "mabel.mcp.get_http_headers",
        lambda **_: {"authorization": f"Bearer {issued.token}"},
    )
    mcp = create_mcp_server(HealthService(), configuration, api_tokens, FakeCapabilities())

    async with Client(mcp) as client:
        result = await client.call_tool("get_identity", {})

    assert result.data == {"owner_id": "owner-a", "authentication": "api_token"}


async def test_mcp_capability_tools_use_the_authenticated_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api_tokens = _api_tokens(tmp_path)
    issued = api_tokens.generate_or_regenerate("owner-a")
    monkeypatch.setattr(
        "mabel.mcp.get_http_headers",
        lambda **_: {"authorization": f"Bearer {issued.token}"},
    )
    mcp = create_mcp_server(
        HealthService(), ConfigurationService(LocalRepository(tmp_path)), api_tokens, FakeCapabilities()
    )

    async with Client(mcp) as client:
        manifest = await client.call_tool("get_mabel_manifest", {})
        index = await client.call_tool("get_capability_index", {})
        run = await client.call_tool("run_capability", {"capability_id": "echo"})

    assert manifest.data == {"repository_commit": "a" * 40}
    assert index.data == {"capabilities": [{"id": "echo"}]}
    assert run.data["result"] == {"message": "hello world"}


async def test_mcp_capability_operation_errors_are_bounded_data_not_tool_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api_tokens = _api_tokens(tmp_path)
    issued = api_tokens.generate_or_regenerate("owner-a")
    monkeypatch.setattr(
        "mabel.mcp.get_http_headers",
        lambda **_: {"authorization": f"Bearer {issued.token}"},
    )
    mcp = create_mcp_server(
        HealthService(), ConfigurationService(LocalRepository(tmp_path)), api_tokens, FailingCapabilities()
    )

    async with Client(mcp) as client:
        index = await client.call_tool("get_capability_index", {})
        run = await client.call_tool(
            "run_capability", {"capability_id": "send_notification", "arguments": {"message": "x"}}
        )

    assert index.data == {"ok": False, "error": {"code": "capability_unavailable"}}
    assert run.data == {
        "ok": False,
        "capability_id": "send_notification",
        "error": {"code": "invalid_arguments"},
    }


async def test_context_binding_candidate_is_selected_only_by_the_mabel_bearer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api_tokens = _api_tokens(tmp_path)
    issued = api_tokens.generate_or_regenerate("owner-a")
    monkeypatch.setattr(
        "mabel.mcp.get_http_headers",
        lambda **_: {"authorization": f"Bearer {issued.token}"},
    )
    mcp = create_mcp_server(
        HealthService(), ConfigurationService(LocalRepository(tmp_path)), api_tokens,
        FakeCapabilities(), FakeContextBindings(),
    )

    async with Client(mcp) as client:
        result = await client.call_tool("get_context_binding_candidate", {})

    assert result.data["mabel_owner_id"] == "owner-a"
    assert result.data["context_repository"]["provider_repository_id"] == "123"
    assert "amie_account_id" not in result.data
