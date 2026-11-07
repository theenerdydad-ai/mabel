from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from mabel.connections.models import Connection
from mabel.connections.credentials import CredentialUnavailableError
from mabel.services.amie_context import AmieContextOperationError, AmieContextOperationService


def _connection(*, enabled: bool = True, credential: bool = True) -> Connection:
    now = datetime.now(timezone.utc)
    return Connection(
        id=uuid4(), owner_id="owner", display_name="AMIE", connection_type="amie",
        transport="mcp_streamable_http", endpoint="https://amie.example.test/mcp",
        credential_id=uuid4() if credential else None, enabled=enabled, created_at=now, updated_at=now,
    )


async def test_context_bridge_uses_the_one_owner_connection_and_never_returns_its_bearer(monkeypatch) -> None:
    connection = _connection()

    class Connections:
        def list(self, owner_id):
            assert owner_id == "owner"
            return [connection]

    class Credentials:
        async def resolve(self, credential_id):
            assert credential_id == str(connection.credential_id)
            return "ace-personal-token-secret"

    calls = []

    class FakeClient:
        def __init__(self, transport):
            self.transport = transport

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return SimpleNamespace(data={"ok": True, "path": arguments["arguments"]["path"]})

    monkeypatch.setattr("mabel.services.amie_context.Client", FakeClient)
    bridge = AmieContextOperationService(Connections(), Credentials())

    result = await bridge.run("owner", "get_file", {"path": "/instructions/start.md"})

    assert result == {"ok": True, "path": "/instructions/start.md"}
    assert calls == [("mabel_context_operation", {
        "operation": "get_file", "arguments": {"path": "/instructions/start.md"},
    })]
    assert "ace-personal-token-secret" not in str(result)


async def test_context_bridge_refuses_ambiguous_or_unconfigured_connections() -> None:
    class Connections:
        def __init__(self, connections):
            self.connections = connections

        def list(self, owner_id):
            return self.connections

    class Credentials:
        async def resolve(self, credential_id):
            return "not-used"

    with pytest.raises(AmieContextOperationError):
        await AmieContextOperationService(Connections([]), Credentials()).run("owner", "get_manifest", {})
    with pytest.raises(AmieContextOperationError):
        await AmieContextOperationService(
            Connections([_connection(), _connection()]), Credentials()
        ).run("owner", "get_manifest", {})


async def test_context_readiness_requires_a_locally_resolvable_credential() -> None:
    connection = _connection()

    class Connections:
        def list(self, owner_id):
            return [connection]

    class Credentials:
        async def resolve(self, credential_id):
            assert credential_id == str(connection.credential_id)
            return "ace-personal-token-secret"

    assert await AmieContextOperationService(Connections(), Credentials()).readiness_error_code("owner") is None


@pytest.mark.parametrize("failure", [CredentialUnavailableError("missing"), ValueError("corrupt")])
async def test_context_readiness_bounds_missing_or_unreadable_credentials(failure) -> None:
    connection = _connection()

    class Connections:
        def list(self, owner_id):
            return [connection]

    class Credentials:
        async def resolve(self, credential_id):
            raise failure

    result = await AmieContextOperationService(Connections(), Credentials()).readiness_error_code("owner")

    assert result == "amie_connection_not_configured"
    assert "missing" not in result and "corrupt" not in result
