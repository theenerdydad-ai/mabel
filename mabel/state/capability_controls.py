"""SQLite persistence for owner-scoped mutable capability controls."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from mabel.state.database import SqliteDatabase


@dataclass(frozen=True)
class CapabilityControl:
    owner_id: str
    capability_id: str
    enabled: bool
    validation_state: str
    validation_error_code: str | None
    last_validated_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CapabilityControlStore(Protocol):
    def get(self, owner_id: str, capability_id: str) -> CapabilityControl | None: ...
    def set_enabled(self, owner_id: str, capability_id: str, enabled: bool, now: datetime) -> CapabilityControl: ...
    def record_validation(
        self, owner_id: str, capability_id: str, state: str, error_code: str | None, now: datetime
    ) -> CapabilityControl: ...


class SqliteCapabilityControlStore:
    """Persist non-secret capability activation and bounded validation evidence."""

    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    def get(self, owner_id: str, capability_id: str) -> CapabilityControl | None:
        with self._database.connect() as database:
            row = database.execute(
                "SELECT * FROM capability_controls WHERE owner_id = ? AND capability_id = ?",
                (owner_id, capability_id),
            ).fetchone()
        return _from_row(row) if row is not None else None

    def set_enabled(self, owner_id: str, capability_id: str, enabled: bool, now: datetime) -> CapabilityControl:
        timestamp = now.isoformat()
        with self._database.connect() as database:
            database.execute(
                """
                INSERT INTO capability_controls (
                    owner_id, capability_id, enabled, validation_state, validation_error_code,
                    last_validated_at, created_at, updated_at
                ) VALUES (?, ?, ?, 'not_run', NULL, NULL, ?, ?)
                ON CONFLICT(owner_id, capability_id) DO UPDATE SET
                    enabled = excluded.enabled, updated_at = excluded.updated_at
                """,
                (owner_id, capability_id, int(enabled), timestamp, timestamp),
            )
        control = self.get(owner_id, capability_id)
        assert control is not None
        return control

    def record_validation(
        self, owner_id: str, capability_id: str, state: str, error_code: str | None, now: datetime
    ) -> CapabilityControl:
        timestamp = now.isoformat()
        with self._database.connect() as database:
            database.execute(
                """
                INSERT INTO capability_controls (
                    owner_id, capability_id, enabled, validation_state, validation_error_code,
                    last_validated_at, created_at, updated_at
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_id, capability_id) DO UPDATE SET
                    validation_state = excluded.validation_state,
                    validation_error_code = excluded.validation_error_code,
                    last_validated_at = excluded.last_validated_at,
                    updated_at = excluded.updated_at
                """,
                (owner_id, capability_id, state, error_code, timestamp, timestamp, timestamp),
            )
        control = self.get(owner_id, capability_id)
        assert control is not None
        return control


def _from_row(row: object) -> CapabilityControl:
    values = row
    return CapabilityControl(
        owner_id=str(values["owner_id"]),
        capability_id=str(values["capability_id"]),
        enabled=bool(values["enabled"]),
        validation_state=str(values["validation_state"]),
        validation_error_code=values["validation_error_code"],
        last_validated_at=(
            datetime.fromisoformat(values["last_validated_at"])
            if values["last_validated_at"] is not None
            else None
        ),
        created_at=datetime.fromisoformat(values["created_at"]),
        updated_at=datetime.fromisoformat(values["updated_at"]),
    )
