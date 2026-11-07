"""SQLite persistence for owner-scoped managed repository metadata."""

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from typing import Protocol
from uuid import UUID

from mabel.repositories.models import ManagedRepository
from mabel.state.database import SqliteDatabase


class ManagedRepositoryStore(Protocol):
    def create(self, repository: ManagedRepository) -> ManagedRepository: ...
    def list_for_owner(self, owner_id: str) -> Sequence[ManagedRepository]: ...
    def get_for_owner(self, repository_id: UUID, owner_id: str) -> ManagedRepository | None: ...
    def update(self, repository: ManagedRepository) -> ManagedRepository: ...
    def delete_for_owner(self, repository_id: UUID, owner_id: str) -> ManagedRepository | None: ...


class SqliteManagedRepositoryStore:
    """Persist repository metadata, never credential plaintext or ciphertext."""

    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    def create(self, repository: ManagedRepository) -> ManagedRepository:
        with self._database.connect() as database:
            database.execute(
                """
                INSERT INTO managed_repositories (
                    id, owner_id, provider, purpose, display_name, provider_repository_id,
                    canonical_locator, default_branch, credential_id, status, enabled,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _values(repository),
            )
        return repository

    def list_for_owner(self, owner_id: str) -> Sequence[ManagedRepository]:
        with self._database.connect() as database:
            rows = database.execute(
                "SELECT * FROM managed_repositories WHERE owner_id = ? ORDER BY created_at, id",
                (owner_id,),
            ).fetchall()
        return [_from_row(row) for row in rows]

    def get_for_owner(self, repository_id: UUID, owner_id: str) -> ManagedRepository | None:
        with self._database.connect() as database:
            row = database.execute(
                "SELECT * FROM managed_repositories WHERE id = ? AND owner_id = ?",
                (str(repository_id), owner_id),
            ).fetchone()
        return _from_row(row) if row is not None else None

    def update(self, repository: ManagedRepository) -> ManagedRepository:
        with self._database.connect() as database:
            result = database.execute(
                """
                UPDATE managed_repositories
                SET display_name = ?, canonical_locator = ?, default_branch = ?, status = ?,
                    enabled = ?, updated_at = ?
                WHERE id = ? AND owner_id = ?
                """,
                (
                    repository.display_name,
                    repository.canonical_locator,
                    repository.default_branch,
                    repository.status,
                    int(repository.enabled),
                    _timestamp(repository.updated_at),
                    str(repository.id),
                    repository.owner_id,
                ),
            )
        if result.rowcount != 1:
            raise KeyError(str(repository.id))
        return repository

    def delete_for_owner(self, repository_id: UUID, owner_id: str) -> ManagedRepository | None:
        repository = self.get_for_owner(repository_id, owner_id)
        if repository is None:
            return None
        with self._database.connect() as database:
            database.execute(
                "DELETE FROM managed_repositories WHERE id = ? AND owner_id = ?",
                (str(repository_id), owner_id),
            )
        return repository


def update_repository(repository: ManagedRepository, **changes: object) -> ManagedRepository:
    return replace(repository, **changes)


def _values(repository: ManagedRepository) -> tuple[object, ...]:
    return (
        str(repository.id), repository.owner_id, repository.provider, repository.purpose,
        repository.display_name, repository.provider_repository_id, repository.canonical_locator,
        repository.default_branch, str(repository.credential_id), repository.status,
        int(repository.enabled), _timestamp(repository.created_at), _timestamp(repository.updated_at),
    )


def _from_row(row: object) -> ManagedRepository:
    values = row
    return ManagedRepository(
        id=UUID(values["id"]), owner_id=values["owner_id"], provider=values["provider"],
        purpose=values["purpose"], display_name=values["display_name"],
        provider_repository_id=values["provider_repository_id"],
        canonical_locator=values["canonical_locator"], default_branch=values["default_branch"],
        credential_id=UUID(values["credential_id"]), status=values["status"],
        enabled=bool(values["enabled"]), created_at=datetime.fromisoformat(values["created_at"]),
        updated_at=datetime.fromisoformat(values["updated_at"]),
    )


def _timestamp(value: datetime) -> str:
    return value.isoformat()
