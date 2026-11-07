from datetime import datetime, timezone
from uuid import uuid4

from mabel.connections.models import Connection
from mabel.state.connections import SqliteConnectionStore
from mabel.state.database import SqliteDatabase


def test_persists_only_owner_scoped_connection_metadata(tmp_path) -> None:
    database = SqliteDatabase(tmp_path / "state.db")
    database.initialize()
    store = SqliteConnectionStore(database)
    now = datetime.now(timezone.utc)
    credential_id = uuid4()
    connection = Connection(
        id=uuid4(),
        owner_id="owner-a",
        display_name="Personal AMIE",
        connection_type="amie",
        transport="mcp_streamable_http",
        endpoint="https://amie.example/mcp",
        credential_id=credential_id,
        enabled=True,
        created_at=now,
        updated_at=now,
    )

    store.create(connection)

    assert store.get_for_owner(connection.id, "owner-a") == connection
    assert store.get_for_owner(connection.id, "owner-b") is None
    assert store.list_for_owner("owner-a") == [connection]
    with database.connect() as raw_database:
        assert raw_database.execute("PRAGMA user_version").fetchone()[0] == 9
        assert raw_database.execute(
            "SELECT credential_id FROM connections WHERE id = ?", (str(connection.id),)
        ).fetchone()["credential_id"] == str(credential_id)


def test_deletes_only_the_owning_connection(tmp_path) -> None:
    database = SqliteDatabase(tmp_path / "state.db")
    database.initialize()
    store = SqliteConnectionStore(database)
    now = datetime.now(timezone.utc)
    connection = Connection(
        id=uuid4(), owner_id="owner-a", display_name="AMIE", connection_type="amie",
        transport="mcp_streamable_http", endpoint="https://amie.example/mcp",
        credential_id=None, enabled=True, created_at=now, updated_at=now,
    )
    store.create(connection)

    assert store.delete_for_owner(connection.id, "owner-b") is False
    assert store.delete_for_owner(connection.id, "owner-a") is True


def test_migration_clears_legacy_plaintext_credential_reference(tmp_path) -> None:
    database = SqliteDatabase(tmp_path / "state.db")
    with database.connect() as raw_database:
        raw_database.execute(
            """
            CREATE TABLE connections (
                id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, display_name TEXT NOT NULL,
                connection_type TEXT NOT NULL, transport TEXT NOT NULL, endpoint TEXT NOT NULL,
                credential_reference TEXT, enabled INTEGER NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
            """
        )
        raw_database.execute(
            """
            INSERT INTO connections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("2e615b5f-9bb5-4fcb-b204-131e7f2d6e22", "owner-a", "AMIE", "amie",
             "mcp_streamable_http", "https://amie.example/mcp", "ace_plaintext", 1,
             "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        raw_database.execute("PRAGMA user_version = 5")

    database.initialize()

    with database.connect() as raw_database:
        row = raw_database.execute("SELECT credential_reference, credential_id FROM connections").fetchone()
    assert row["credential_reference"] is None
    assert row["credential_id"] is None
