"""SQLite implementation of owner-scoped connection persistence."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from mabel.connections.models import Connection
from mabel.state.database import SqliteDatabase


class ConnectionStore(Protocol):
    def create(self, connection: Connection) -> Connection: ...
    def list_for_owner(self, owner_id: str) -> Sequence[Connection]: ...
    def get_for_owner(self, connection_id: UUID, owner_id: str) -> Connection | None: ...
    def update(self, connection: Connection) -> Connection: ...
    def delete_for_owner(self, connection_id: UUID, owner_id: str) -> bool: ...


class SqliteConnectionStore:
    """Persist connection metadata only; credential values never enter this store."""

    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    def create(self, connection: Connection) -> Connection:
        with self._database.connect() as database:
            database.execute(
                """
                INSERT INTO connections (
                    id, owner_id, display_name, connection_type, transport, endpoint,
                    credential_id, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _connection_values(connection),
            )
        return connection

    def list_for_owner(self, owner_id: str) -> Sequence[Connection]:
        with self._database.connect() as database:
            rows = database.execute(
                "SELECT * FROM connections WHERE owner_id = ? ORDER BY created_at, id", (owner_id,)
            ).fetchall()
        return [_connection_from_row(row) for row in rows]

    def get_for_owner(self, connection_id: UUID, owner_id: str) -> Connection | None:
        with self._database.connect() as database:
            row = database.execute(
                "SELECT * FROM connections WHERE id = ? AND owner_id = ?", (str(connection_id), owner_id)
            ).fetchone()
        return _connection_from_row(row) if row is not None else None

    def update(self, connection: Connection) -> Connection:
        with self._database.connect() as database:
            result = database.execute(
                """
                UPDATE connections
                SET display_name = ?, endpoint = ?, credential_id = ?, enabled = ?, updated_at = ?
                WHERE id = ? AND owner_id = ?
                """,
                (
                    connection.display_name,
                    connection.endpoint,
                    str(connection.credential_id) if connection.credential_id is not None else None,
                    int(connection.enabled),
                    _timestamp(connection.updated_at),
                    str(connection.id),
                    connection.owner_id,
                ),
            )
        if result.rowcount != 1:
            raise KeyError(str(connection.id))
        return connection

    def delete_for_owner(self, connection_id: UUID, owner_id: str) -> bool:
        with self._database.connect() as database:
            result = database.execute(
                "DELETE FROM connections WHERE id = ? AND owner_id = ?", (str(connection_id), owner_id)
            )
        return result.rowcount == 1


def _connection_values(connection: Connection) -> tuple[object, ...]:
    return (
        str(connection.id),
        connection.owner_id,
        connection.display_name,
        connection.connection_type,
        connection.transport,
        connection.endpoint,
        str(connection.credential_id) if connection.credential_id is not None else None,
        int(connection.enabled),
        _timestamp(connection.created_at),
        _timestamp(connection.updated_at),
    )


def _connection_from_row(row: object) -> Connection:
    values = row  # sqlite Row has mapping access but avoids a runtime dependency in callers.
    return Connection(
        id=UUID(values["id"]),
        owner_id=values["owner_id"],
        display_name=values["display_name"],
        connection_type=values["connection_type"],
        transport=values["transport"],
        endpoint=values["endpoint"],
        credential_id=UUID(values["credential_id"]) if values["credential_id"] is not None else None,
        enabled=bool(values["enabled"]),
        created_at=datetime.fromisoformat(values["created_at"]),
        updated_at=datetime.fromisoformat(values["updated_at"]),
    )


def _timestamp(value: datetime) -> str:
    return value.isoformat()
