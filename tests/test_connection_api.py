from httpx import ASGITransport, AsyncClient

from mabel.app import create_app
from mabel.config import Settings


async def test_connection_api_encrypts_amie_token_and_never_serializes_it(tmp_path) -> None:
    token = "ace_connection-token-for-test-only"
    settings = Settings(state_database_path=tmp_path / "state.db", development_owner_id="test-owner")
    app = create_app(settings=settings)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            created = await client.post(
                "/api/connections",
                json={
                    "display_name": "Personal AMIE",
                    "connection_type": "amie",
                    "transport": "mcp_streamable_http",
                    "endpoint": "https://amie.example/mcp",
                    "amie_api_token": token,
                },
            )
            listed = await client.get("/api/connections")

        with app.state.database.connect() as database:
            stored = database.execute("SELECT encrypted_token FROM amie_connection_credentials").fetchone()
            metadata = database.execute("SELECT credential_id FROM connections").fetchone()

    assert created.status_code == 201
    assert created.json()["credential_configured"] is True
    assert token not in created.text
    assert "amie_api_token" not in created.json()
    assert "credential_id" not in created.json()
    assert listed.json()[0]["id"] == created.json()["id"]
    assert token not in stored["encrypted_token"]
    assert metadata["credential_id"] is not None


async def test_connection_api_replaces_or_removes_amie_token(tmp_path) -> None:
    settings = Settings(state_database_path=tmp_path / "state.db", development_owner_id="test-owner")
    app = create_app(settings=settings)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            created = await client.post(
                "/api/connections",
                json={
                    "display_name": "AMIE", "connection_type": "amie",
                    "transport": "mcp_streamable_http", "endpoint": "https://amie.example/mcp",
                    "amie_api_token": "ace_original-token",
                },
            )
            connection_id = created.json()["id"]
            replaced = await client.patch(
                f"/api/connections/{connection_id}", json={"amie_api_token": "ace_replacement-token"}
            )
            removed = await client.patch(
                f"/api/connections/{connection_id}", json={"clear_amie_api_token": True}
            )

    assert replaced.status_code == 200
    assert "ace_replacement-token" not in replaced.text
    assert replaced.json()["credential_configured"] is True
    assert removed.status_code == 200
    assert removed.json()["credential_configured"] is False


async def test_connection_api_rejects_non_amie_tokens_without_retaining_them(tmp_path) -> None:
    app = create_app(
        settings=Settings(state_database_path=tmp_path / "state.db", development_owner_id="test-owner")
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            rejected = await client.post(
                "/api/connections",
                json={
                    "display_name": "AMIE", "connection_type": "amie",
                    "transport": "mcp_streamable_http", "endpoint": "https://amie.example/mcp",
                    "amie_api_token": "github_pat_not-an-amie-token",
                },
            )
        with app.state.database.connect() as database:
            credential_count = database.execute(
                "SELECT COUNT(*) FROM amie_connection_credentials"
            ).fetchone()[0]

    assert rejected.status_code == 422
    assert credential_count == 0


async def test_connection_api_rejects_owner_id_and_disabled_testing(tmp_path) -> None:
    app = create_app(
        settings=Settings(state_database_path=tmp_path / "state.db", development_owner_id="test-owner")
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            rejected = await client.post(
                "/api/connections",
                json={
                    "owner_id": "other-owner", "display_name": "AMIE", "connection_type": "amie",
                    "transport": "mcp_streamable_http", "endpoint": "https://amie.example/mcp",
                },
            )
            created = await client.post(
                "/api/connections",
                json={
                    "display_name": "AMIE", "connection_type": "amie",
                    "transport": "mcp_streamable_http", "endpoint": "https://amie.example/mcp",
                },
            )
            connection_id = created.json()["id"]
            await client.patch(f"/api/connections/{connection_id}", json={"enabled": False})
            tested = await client.post(f"/api/connections/{connection_id}/test")

    assert rejected.status_code == 422
    assert tested.status_code == 409
