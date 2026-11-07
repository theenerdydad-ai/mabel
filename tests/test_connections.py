from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest

from mabel.amie.models import AmieFile, DiscoveredPath
from mabel.connections.credentials import AmieConnectionCredentialStore, CredentialUnavailableError
from mabel.connections.models import Connection
from mabel.connections import readers
from mabel.connections.readers import AmieMcpStreamableHttpReaderFactory
from mabel.services.connections import (
    ConnectionDisabledError,
    ConnectionService,
    ConnectionValidationError,
)
from mabel.state.connections import SqliteConnectionStore
from mabel.state.database import SqliteDatabase


@dataclass
class FakeAmieReader:
    root_manifest: Mapping[str, Any]

    async def read_bootstrap(self) -> AmieFile:
        return AmieFile(
            path=DiscoveredPath.from_metadata("/instructions/start.md", "bootstrap"),
            content="# Start\n", sha="bootstrap-sha", raw={},
        )

    async def read_root_manifest(self) -> Mapping[str, Any]:
        return self.root_manifest

    async def read_file(self, path: DiscoveredPath) -> AmieFile:
        raise AssertionError("Connection testing must not traverse nested files.")


class FakeReaderFactory:
    def __init__(self, reader: FakeAmieReader) -> None:
        self.reader = reader
        self.opened: list[Connection] = []

    @asynccontextmanager
    async def open(self, connection: Connection) -> AsyncIterator[FakeAmieReader]:
        self.opened.append(connection)
        yield self.reader


class CredentialUnavailableFactory:
    @asynccontextmanager
    async def open(self, connection: Connection) -> AsyncIterator[FakeAmieReader]:
        raise CredentialUnavailableError("secret provider unavailable")
        yield  # pragma: no cover - establishes this as an async context manager.


def service(tmp_path) -> tuple[ConnectionService, FakeReaderFactory, AmieConnectionCredentialStore]:
    database = SqliteDatabase(tmp_path / "state.db")
    database.initialize()
    credentials = AmieConnectionCredentialStore(database, "test-server-secret-at-least-thirty-two-characters")
    factory = FakeReaderFactory(
        FakeAmieReader(
            {"projects": [{"id": "projects", "path": "/projects"}], "sessions": [{"id": "sessions", "path": "/sessions"}]}
        )
    )
    return ConnectionService(SqliteConnectionStore(database), factory, credentials), factory, credentials


async def test_tests_only_bootstrap_and_root_manifest(tmp_path) -> None:
    connections, factory, _ = service(tmp_path)
    connection = connections.create(
        owner_id="owner-a", display_name="AMIE", connection_type="amie",
        transport="mcp_streamable_http", endpoint="https://amie.example/mcp",
    )

    result = await connections.test(connection.id, "owner-a")

    assert result.as_dict() == {
        "ok": True,
        "connection_id": str(connection.id),
        "bootstrap": {"loaded": True, "sha": "bootstrap-sha"},
        "root_manifest": {"loaded": True, "collection_count": 2},
    }
    assert factory.opened == [connection]


async def test_disabled_connections_cannot_be_tested(tmp_path) -> None:
    connections, _, _ = service(tmp_path)
    connection = connections.create(
        owner_id="owner-a", display_name="AMIE", connection_type="amie",
        transport="mcp_streamable_http", endpoint="https://amie.example/mcp",
    )
    connections.update(connection.id, "owner-a", enabled=False)

    with pytest.raises(ConnectionDisabledError):
        await connections.test(connection.id, "owner-a")


async def test_connection_test_does_not_expose_credential_failure_details(tmp_path) -> None:
    database = SqliteDatabase(tmp_path / "state.db")
    database.initialize()
    credentials = AmieConnectionCredentialStore(database, "test-server-secret-at-least-thirty-two-characters")
    connections = ConnectionService(
        SqliteConnectionStore(database), CredentialUnavailableFactory(), credentials
    )
    connection = connections.create(
        owner_id="owner-a", display_name="AMIE", connection_type="amie",
        transport="mcp_streamable_http", endpoint="https://amie.example/mcp",
        amie_api_token="ace_valid-token",
    )

    result = await connections.test(connection.id, "owner-a")

    assert result.as_dict() == {
        "ok": False,
        "connection_id": str(connection.id),
        "error_code": "credential_unavailable",
    }


async def test_encrypted_token_is_available_only_to_the_trusted_reader(tmp_path, monkeypatch) -> None:
    database = SqliteDatabase(tmp_path / "state.db")
    database.initialize()
    credentials = AmieConnectionCredentialStore(database, "test-server-secret-at-least-thirty-two-characters")
    token = "ace_token-used-by-trusted-transport"
    credential_id = credentials.store("owner-a", token)

    assert await credentials.resolve(str(credential_id)) == token
    with database.connect() as raw_database:
        encrypted = raw_database.execute(
            "SELECT encrypted_token FROM amie_connection_credentials WHERE id = ?", (str(credential_id),)
        ).fetchone()["encrypted_token"]
    assert token not in encrypted

    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, transport: object) -> None:
            captured["transport"] = transport

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    def fake_transport(endpoint: str, *, auth: object) -> object:
        captured["endpoint"] = endpoint
        captured["auth"] = auth
        return object()

    monkeypatch.setattr(readers, "Client", FakeClient)
    monkeypatch.setattr(readers, "StreamableHttpTransport", fake_transport)
    connection = Connection(
        id=uuid4(), owner_id="owner-a", display_name="AMIE", connection_type="amie",
        transport="mcp_streamable_http", endpoint="https://amie.example/mcp",
        credential_id=credential_id, enabled=True,
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )
    factory = AmieMcpStreamableHttpReaderFactory(credentials)
    async with factory.open(connection):
        pass

    # The reader receives only a metadata UUID. The factory resolves that ID in
    # trusted code and passes the transient raw token to FastMCP, which creates
    # the outbound Bearer authorization header.
    assert captured["endpoint"] == connection.endpoint
    assert captured["auth"] == token
    assert captured["transport"] is not None


def test_rejects_unsupported_connection_or_unsafe_endpoint(tmp_path) -> None:
    connections, _, _ = service(tmp_path)
    with pytest.raises(ConnectionValidationError, match="Only AMIE"):
        connections.create(
            owner_id="owner-a", display_name="Other", connection_type="other",
            transport="mcp_streamable_http", endpoint="https://example.test",
        )
    with pytest.raises(ConnectionValidationError, match="absolute HTTP"):
        connections.create(
            owner_id="owner-a", display_name="AMIE", connection_type="amie",
            transport="mcp_streamable_http", endpoint="file:///secret",
        )
    with pytest.raises(ConnectionValidationError, match="must not contain credentials"):
        connections.create(
            owner_id="owner-a", display_name="AMIE", connection_type="amie",
            transport="mcp_streamable_http", endpoint="https://token@amie.example/mcp",
        )
    with pytest.raises(ValueError, match="ace_"):
        connections.create(
            owner_id="owner-a", display_name="AMIE", connection_type="amie",
            transport="mcp_streamable_http", endpoint="https://amie.example/mcp",
            amie_api_token="github_pat_wrong-token",
        )
