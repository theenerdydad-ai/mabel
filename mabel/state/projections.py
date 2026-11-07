"""Live metadata for synchronized user MABEL context-repository projections."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from mabel.state.database import SqliteDatabase


@dataclass(frozen=True)
class RepositoryProjectionRecord:
    repository_id: UUID
    commit_sha: str
    synced_at: datetime
    status: str
    error_code: str | None = None


class SqliteRepositoryProjectionStore:
    """Persist projection metadata only, never a checkout path or credential."""

    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    def record_success(self, repository_id: UUID, commit_sha: str, synced_at: datetime) -> None:
        with self._database.connect() as database:
            database.execute(
                """
                INSERT INTO repository_projections (repository_id, commit_sha, synced_at, status, error_code)
                VALUES (?, ?, ?, 'synchronized', NULL)
                ON CONFLICT(repository_id) DO UPDATE SET
                    commit_sha = excluded.commit_sha, synced_at = excluded.synced_at,
                    status = excluded.status, error_code = NULL
                """,
                (str(repository_id), commit_sha, synced_at.isoformat()),
            )
