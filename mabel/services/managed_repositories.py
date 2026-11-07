"""Managed repository registration and verification lifecycle."""

import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from typing import Final
from uuid import UUID, uuid4

from mabel.connections.credentials import CredentialUnavailableError
from mabel.repositories.credentials import GitHubTokenCredential, ManagedRepositoryCredentialStore
from mabel.repositories.github import (
    RepositoryAuthenticationError,
    RepositoryProvider,
    RepositoryProviderError,
)
from mabel.repositories.models import (
    ManagedRepository,
    ManagedRepositoryTestResult,
    RepositoryVerification,
)
from mabel.state.managed_repositories import ManagedRepositoryStore


class ManagedRepositoryError(ValueError):
    pass


class ManagedRepositoryNotFoundError(ManagedRepositoryError):
    pass


class ManagedRepositoryDisabledError(ManagedRepositoryError):
    pass


class ManagedRepositoryValidationError(ManagedRepositoryError):
    pass


class ManagedRepositoryConflictError(ManagedRepositoryError):
    pass


_UNSET: Final = object()
MABEL_CONTEXT_REPOSITORY_PURPOSE: Final = "mabel_context_repository"


class ManagedRepositoryService:
    """Own lifecycle semantics; HTTP and the portal only adapt this service."""

    def __init__(
        self,
        store: ManagedRepositoryStore,
        credentials: ManagedRepositoryCredentialStore,
        github: RepositoryProvider,
    ) -> None:
        self._store = store
        self._credentials = credentials
        self._github = github

    async def register(
        self,
        *,
        owner_id: str,
        display_name: str,
        provider: str,
        purpose: str,
        repository: str,
        github_token: str,
    ) -> ManagedRepository:
        self._validate_registration(owner_id, display_name, provider, purpose)
        verification = await self._verify_token(repository, github_token)
        credential_id = self._credentials.store(owner_id, github_token)
        now = _now()
        managed_repository = ManagedRepository(
            id=uuid4(), owner_id=owner_id, provider=provider, purpose=purpose,
            display_name=display_name.strip(),
            provider_repository_id=verification.provider_repository_id,
            canonical_locator=verification.canonical_locator,
            default_branch=verification.default_branch, credential_id=credential_id,
            status="connected", enabled=True, created_at=now, updated_at=now,
        )
        try:
            return self._store.create(managed_repository)
        except sqlite3.IntegrityError as error:
            self._credentials.delete(credential_id, owner_id)
            raise ManagedRepositoryConflictError("This repository is already registered.") from error

    def list(self, owner_id: str) -> list[ManagedRepository]:
        return list(self._store.list_for_owner(owner_id))

    def get(self, repository_id: UUID, owner_id: str) -> ManagedRepository:
        repository = self._store.get_for_owner(repository_id, owner_id)
        if repository is None:
            raise ManagedRepositoryNotFoundError("Managed repository was not found.")
        return repository

    def resolve_active_context_repository(self, owner_id: str) -> ManagedRepository:
        """Resolve exactly one enabled, connected context repository for execution."""
        active = [
            repository for repository in self.list(owner_id)
            if repository.purpose == MABEL_CONTEXT_REPOSITORY_PURPOSE
            and repository.enabled and repository.status == "connected"
        ]
        if not active:
            raise ManagedRepositoryNotFoundError(
                "No active MABEL context repository is registered."
            )
        if len(active) > 1:
            raise ManagedRepositoryConflictError(
                "Multiple active MABEL context repositories are registered. Disable all but one."
            )
        return active[0]

    def update(
        self,
        repository_id: UUID,
        owner_id: str,
        *,
        display_name: str | object = _UNSET,
        enabled: bool | object = _UNSET,
    ) -> ManagedRepository:
        current = self.get(repository_id, owner_id)
        updated = replace(
            current,
            display_name=current.display_name if display_name is _UNSET else str(display_name).strip(),
            enabled=current.enabled if enabled is _UNSET else bool(enabled),
            updated_at=_now(),
        )
        if not updated.display_name:
            raise ManagedRepositoryValidationError("Managed repository display name is required.")
        return self._store.update(updated)

    async def test(self, repository_id: UUID, owner_id: str) -> ManagedRepositoryTestResult:
        repository = self.get(repository_id, owner_id)
        if not repository.enabled:
            raise ManagedRepositoryDisabledError("Disabled managed repositories cannot be tested.")
        try:
            verification = await self._verify_stored(repository)
        except CredentialUnavailableError:
            return _failed(repository.id, "credential_unavailable")
        except RepositoryAuthenticationError:
            self._mark_status(repository, "needs_reauthentication")
            return _failed(repository.id, "authentication_failed")
        except RepositoryProviderError as error:
            return _failed(repository.id, error.error_code)
        if verification.provider_repository_id != repository.provider_repository_id:
            self._mark_status(repository, "identity_mismatch")
            return _failed(repository.id, "identity_mismatch")
        self._store.update(replace(
            repository,
            canonical_locator=verification.canonical_locator,
            default_branch=verification.default_branch,
            status="connected",
            updated_at=_now(),
        ))
        return _success(repository.id, verification)

    async def reconnect(
        self, repository_id: UUID, owner_id: str, github_token: str
    ) -> ManagedRepository:
        repository = self.get(repository_id, owner_id)
        try:
            verification = await self._verify_token(repository.canonical_locator, github_token)
        except RepositoryAuthenticationError:
            self._mark_status(repository, "needs_reauthentication")
            raise
        if verification.provider_repository_id != repository.provider_repository_id:
            self._mark_status(repository, "identity_mismatch")
            raise ManagedRepositoryConflictError(
                "This token resolves to a different repository. Register it separately."
            )
        self._credentials.replace(repository.credential_id, owner_id, github_token)
        return self._store.update(replace(
            repository,
            canonical_locator=verification.canonical_locator,
            default_branch=verification.default_branch,
            status="connected",
            updated_at=_now(),
        ))

    def delete(self, repository_id: UUID, owner_id: str) -> None:
        repository = self._store.delete_for_owner(repository_id, owner_id)
        if repository is None:
            raise ManagedRepositoryNotFoundError("Managed repository was not found.")
        self._credentials.delete(repository.credential_id, owner_id)

    async def _verify_stored(self, repository: ManagedRepository) -> RepositoryVerification:
        credential = await self._credentials.resolve(str(repository.credential_id))
        if not isinstance(credential, GitHubTokenCredential):
            raise CredentialUnavailableError("Repository credential is not a GitHub token.")
        return await self._github.verify(repository.canonical_locator, credential)

    async def _verify_token(self, repository: str, github_token: str) -> RepositoryVerification:
        return await self._github.verify(repository, GitHubTokenCredential(_required_token(github_token)))

    @staticmethod
    def _validate_registration(owner_id: str, display_name: str, provider: str, purpose: str) -> None:
        if not owner_id.strip():
            raise ManagedRepositoryValidationError("Managed repository owner is required.")
        if not display_name.strip():
            raise ManagedRepositoryValidationError("Managed repository display name is required.")
        if provider != "github" or purpose != MABEL_CONTEXT_REPOSITORY_PURPOSE:
            raise ManagedRepositoryValidationError(
                "Only GitHub MABEL context repositories are supported."
            )

    def _mark_status(self, repository: ManagedRepository, status: str) -> None:
        self._store.update(replace(repository, status=status, updated_at=_now()))


def _required_token(token: str) -> str:
    cleaned = token.strip()
    if not cleaned:
        raise ManagedRepositoryValidationError("GitHub token is required.")
    return cleaned


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _success(repository_id: UUID, verification: RepositoryVerification) -> ManagedRepositoryTestResult:
    return ManagedRepositoryTestResult(
        ok=True, repository_id=repository_id,
        provider_repository_id=verification.provider_repository_id,
        canonical_locator=verification.canonical_locator,
        default_branch=verification.default_branch,
    )


def _failed(repository_id: UUID, error_code: str) -> ManagedRepositoryTestResult:
    return ManagedRepositoryTestResult(ok=False, repository_id=repository_id, error_code=error_code)
