"""SQLite persistence for owner-scoped MABEL API-token hashes."""

from datetime import datetime
from typing import Protocol
from uuid import uuid4

from mabel.auth.models import ApiIdentity
from mabel.state.database import SqliteDatabase


class ApiTokenStore(Protocol):
    """Persistence boundary that deliberately accepts token hashes, never plaintext."""

    def replace_for_owner(self, owner_id: str, token_hash: str, created_at: datetime) -> None: ...
    def identity_for_hash(self, token_hash: str) -> ApiIdentity | None: ...
    def has_token_for_owner(self, owner_id: str) -> bool: ...


class SqliteApiTokenStore:
    """Store exactly one irreversible bearer-token hash per owner."""

    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    def replace_for_owner(self, owner_id: str, token_hash: str, created_at: datetime) -> None:
        """Atomically replace the owner's only active token hash."""
        with self._database.connect() as database:
            database.execute(
                """
                INSERT INTO api_tokens (id, owner_id, token_hash, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(owner_id) DO UPDATE SET
                    id = excluded.id,
                    token_hash = excluded.token_hash,
                    created_at = excluded.created_at
                """,
                (str(uuid4()), owner_id, token_hash, created_at.isoformat()),
            )

    def identity_for_hash(self, token_hash: str) -> ApiIdentity | None:
        with self._database.connect() as database:
            row = database.execute(
                "SELECT owner_id FROM api_tokens WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        return ApiIdentity(owner_id=row["owner_id"]) if row is not None else None

    def has_token_for_owner(self, owner_id: str) -> bool:
        with self._database.connect() as database:
            row = database.execute(
                "SELECT 1 FROM api_tokens WHERE owner_id = ?", (owner_id,)
            ).fetchone()
        return row is not None
