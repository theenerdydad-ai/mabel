from dataclasses import dataclass

import pytest

from mabel.config import Settings
from mabel.repositories.credentials import ManagedRepositoryCredentialStore
from mabel.repositories.github import RepositoryAuthenticationError
from mabel.repositories.models import RepositoryVerification
from mabel.services.managed_repositories import (
    ManagedRepositoryConflictError,
    ManagedRepositoryService,
)
from mabel.state.database import SqliteDatabase
from mabel.state.managed_repositories import SqliteManagedRepositoryStore


@dataclass
class FakeGitHub:
    verification: RepositoryVerification
    fail_authentication: bool = False

    async def verify(self, locator, credential):
        if self.fail_authentication:
            raise RepositoryAuthenticationError("rejected")
        return self.verification


def service(tmp_path, github=None):
    database = SqliteDatabase(tmp_path / "state.db")
    database.initialize()
    credentials = ManagedRepositoryCredentialStore(database, Settings().server_secret)
    github = github or FakeGitHub(RepositoryVerification("123", "owner/capabilities", "main"))
    return (
        ManagedRepositoryService(SqliteManagedRepositoryStore(database), credentials, github),
        credentials,
        database,
    )


async def test_registers_verified_repository_and_encrypts_token_separately(tmp_path) -> None:
    repositories, credentials, database = service(tmp_path)

    repository = await repositories.register(
        owner_id="owner-a", display_name="Capabilities", provider="github",
        purpose="mabel_context_repository", repository="owner/capabilities", github_token="github-token-value",
    )

    assert repository.provider_repository_id == "123"
    assert repository.canonical_locator == "owner/capabilities"
    assert repository.credential_id
    with database.connect() as raw_database:
        encrypted = raw_database.execute(
            "SELECT encrypted_secret FROM managed_repository_credentials WHERE id = ?",
            (str(repository.credential_id),),
        ).fetchone()[0]
    assert encrypted != "github-token-value"
    resolved = await credentials.resolve(str(repository.credential_id))
    assert resolved.token == "github-token-value"


async def test_reconnect_replaces_credential_only_after_same_identity_verification(tmp_path) -> None:
    repositories, credentials, _ = service(tmp_path)
    repository = await repositories.register(
        owner_id="owner-a", display_name="Capabilities", provider="github",
        purpose="mabel_context_repository", repository="owner/capabilities", github_token="old-token",
    )

    await repositories.reconnect(repository.id, "owner-a", "replacement-token")
    assert (await credentials.resolve(str(repository.credential_id))).token == "replacement-token"

    repositories._github.verification = RepositoryVerification("different", "owner/other", "main")
    with pytest.raises(ManagedRepositoryConflictError, match="different repository"):
        await repositories.reconnect(repository.id, "owner-a", "wrong-token")

    assert (await credentials.resolve(str(repository.credential_id))).token == "replacement-token"
    assert repositories.get(repository.id, "owner-a").status == "identity_mismatch"


async def test_owner_scoped_uniqueness_rejects_duplicate_but_allows_another_owner(tmp_path) -> None:
    repositories, _, _ = service(tmp_path)
    arguments = {
        "display_name": "Capabilities", "provider": "github", "purpose": "mabel_context_repository",
        "repository": "owner/capabilities", "github_token": "token",
    }
    await repositories.register(owner_id="owner-a", **arguments)
    with pytest.raises(ManagedRepositoryConflictError):
        await repositories.register(owner_id="owner-a", **arguments)
    assert (await repositories.register(owner_id="owner-b", **arguments)).owner_id == "owner-b"


async def test_failed_reconnect_marks_existing_repository_for_reauthentication(tmp_path) -> None:
    github = FakeGitHub(RepositoryVerification("123", "owner/capabilities", "main"))
    repositories, _, _ = service(tmp_path, github)
    repository = await repositories.register(
        owner_id="owner-a", display_name="Capabilities", provider="github",
        purpose="mabel_context_repository", repository="owner/capabilities", github_token="token",
    )
    github.fail_authentication = True

    with pytest.raises(RepositoryAuthenticationError):
        await repositories.reconnect(repository.id, "owner-a", "revoked-token")

    assert repositories.get(repository.id, "owner-a").status == "needs_reauthentication"


async def test_execution_requires_one_unambiguous_active_context_repository(tmp_path) -> None:
    github = FakeGitHub(RepositoryVerification("123", "owner/first", "main"))
    repositories, _, _ = service(tmp_path, github)
    first = await repositories.register(
        owner_id="owner-a", display_name="First", provider="github",
        purpose="mabel_context_repository", repository="owner/first", github_token="token",
    )
    github.verification = RepositoryVerification("456", "owner/second", "main")
    await repositories.register(
        owner_id="owner-a", display_name="Second", provider="github",
        purpose="mabel_context_repository", repository="owner/second", github_token="token",
    )

    with pytest.raises(ManagedRepositoryConflictError, match="Multiple active"):
        repositories.resolve_active_context_repository("owner-a")
    repositories.update(first.id, "owner-a", enabled=False)
    assert repositories.resolve_active_context_repository("owner-a").canonical_locator == "owner/second"
