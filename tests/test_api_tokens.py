import hashlib
import sqlite3

from httpx import ASGITransport, AsyncClient

from mabel.app import create_app
from mabel.config import Settings
from mabel.state.database import SqliteDatabase


async def test_token_is_returned_once_and_only_its_hash_is_persisted(tmp_path) -> None:
    database_path = tmp_path / "state.db"
    app = create_app(settings=Settings(state_database_path=database_path))

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            status = await client.get("/api/access/token-status")
            created = await client.post("/api/access/token")
            after = await client.get("/api/access/token-status")

    assert status.json() == {"has_token": False}
    assert created.status_code == 200
    token = created.json()["token"]
    assert token.startswith("mabel_")
    assert after.json() == {"has_token": True}
    assert "mabel_" not in after.text

    with sqlite3.connect(database_path) as database:
        row = database.execute("SELECT owner_id, token_hash FROM api_tokens").fetchone()
    assert row == ("local-development", hashlib.sha256(token.encode()).hexdigest())
    assert token not in str(row)


async def test_regeneration_immediately_invalidates_the_previous_bearer(tmp_path) -> None:
    app = create_app(settings=Settings(state_database_path=tmp_path / "state.db"))

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            first = (await client.post("/api/access/token")).json()["token"]
            before = await client.get(
                "/api/machine/identity", headers={"Authorization": f"Bearer {first}"}
            )
            second = (await client.post("/api/access/token")).json()["token"]
            invalidated = await client.get(
                "/api/machine/identity", headers={"Authorization": f"Bearer {first}"}
            )
            current = await client.get(
                "/api/machine/identity", headers={"Authorization": f"Bearer {second}"}
            )

    assert first != second
    assert before.json() == {"owner_id": "local-development", "authentication": "api_token"}
    assert invalidated.status_code == 401
    assert current.json() == {"owner_id": "local-development", "authentication": "api_token"}


async def test_machine_identity_rejects_missing_and_non_mabel_bearers(tmp_path) -> None:
    app = create_app(settings=Settings(state_database_path=tmp_path / "state.db"))

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            missing = await client.get("/api/machine/identity")
            wrong = await client.get(
                "/api/machine/identity", headers={"Authorization": "Bearer amie_not_mabel"}
            )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"


async def test_database_version_two_migrates_to_current_version_with_token_storage(tmp_path) -> None:
    database_path = tmp_path / "state.db"
    with sqlite3.connect(database_path) as database:
        database.execute("PRAGMA user_version = 2")

    app = create_app(settings=Settings(state_database_path=database_path))

    # Initialization is intentionally the only migration trigger.
    async with app.router.lifespan_context(app):
        pass

    with sqlite3.connect(database_path) as database:
        version = database.execute("PRAGMA user_version").fetchone()[0]
        table = database.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'api_tokens'"
        ).fetchone()
        capability_controls = database.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'capability_controls'"
        ).fetchone()
    assert version == 9
    assert table == ("api_tokens",)
    assert capability_controls == ("capability_controls",)


def test_version_six_migrates_capability_controls_with_owner_scoped_primary_key(tmp_path) -> None:
    database = SqliteDatabase(tmp_path / "state.db")
    with database.connect() as connection:
        connection.execute("PRAGMA user_version = 6")

    database.initialize()
    database.initialize()

    with database.connect() as connection:
        columns = connection.execute("PRAGMA table_info(capability_controls)").fetchall()
    assert {column["name"] for column in columns} >= {"owner_id", "capability_id", "enabled"}
    assert {column["name"] for column in columns if column["pk"]} == {"owner_id", "capability_id"}
