"""Persistence for externally authenticated identities bound to MABEL owners."""

from datetime import datetime, timezone

from mabel.state.database import SqliteDatabase


class OwnerIdentityConflictError(RuntimeError):
    """Raised when an explicit identity binding conflicts with an existing one."""


class SqliteOwnerIdentityStore:
    """Store only immutable external subjects and opaque MABEL owner identifiers."""

    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    def owner_for(self, provider: str, subject: str) -> str | None:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT owner_id FROM owner_identities WHERE provider = ? AND subject = ?",
                (provider, subject),
            ).fetchone()
        return None if row is None else str(row["owner_id"])

    def subject_for(self, provider: str, owner_id: str) -> str | None:
        """Return the immutable external subject already bound to this owner."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT subject FROM owner_identities WHERE provider = ? AND owner_id = ?",
                (provider, owner_id),
            ).fetchone()
        return None if row is None else str(row["subject"])

    def bind(self, provider: str, subject: str, owner_id: str) -> None:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT owner_id FROM owner_identities WHERE provider = ? AND subject = ?",
                (provider, subject),
            ).fetchone()
            if row is not None:
                if row["owner_id"] != owner_id:
                    raise OwnerIdentityConflictError("The AMIE subject is already bound to another MABEL owner.")
                return
            owner_row = connection.execute(
                "SELECT subject FROM owner_identities WHERE provider = ? AND owner_id = ?",
                (provider, owner_id),
            ).fetchone()
            if owner_row is not None:
                raise OwnerIdentityConflictError("The MABEL owner is already bound to another AMIE subject.")
            connection.execute(
                "INSERT INTO owner_identities (provider, subject, owner_id, created_at) VALUES (?, ?, ?, ?)",
                (provider, subject, owner_id, datetime.now(timezone.utc).isoformat()),
            )
