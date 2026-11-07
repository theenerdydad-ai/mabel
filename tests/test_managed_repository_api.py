from httpx import ASGITransport, AsyncClient

from mabel.app import create_app
from mabel.config import Settings
from mabel.repositories.models import RepositoryVerification
from mabel.services.managed_repositories import ManagedRepositoryService
from mabel.state.database import SqliteDatabase
from mabel.state.managed_repositories import SqliteManagedRepositoryStore
from mabel.repositories.credentials import ManagedRepositoryCredentialStore


class FakeGitHub:
    async def verify(self, locator, credential):
        return RepositoryVerification("123", "owner/capabilities", "main")


async def test_repository_api_never_returns_submitted_token_or_internal_credential_id(tmp_path) -> None:
    database = SqliteDatabase(tmp_path / "state.db")
    settings = Settings(state_database_path=tmp_path / "state.db", development_owner_id="test-owner")
    service = ManagedRepositoryService(
        SqliteManagedRepositoryStore(database),
        ManagedRepositoryCredentialStore(database, settings.server_secret),
        FakeGitHub(),
    )
    app = create_app(
        managed_repository_service=service,
        settings=settings,
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            created = await client.post(
                "/api/managed-repositories",
                json={
                    "display_name": "Capabilities", "provider": "github",
                    "purpose": "mabel_context_repository", "repository": "owner/capabilities",
                    "github_token": "github-token-value",
                },
            )
            listed = await client.get("/api/managed-repositories")

    assert created.status_code == 201
    assert "github-token-value" not in created.text
    assert "credential_id" not in created.json()
    assert "github_token" not in listed.text
    assert listed.json()[0]["credential_configured"] is True
