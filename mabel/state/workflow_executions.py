"""Durable, owner-scoped workflow-execution state."""

import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Protocol, Sequence

from mabel.state.database import SqliteDatabase
from mabel.workflows.models import WorkflowExecution


class WorkflowExecutionStoreError(RuntimeError):
    """Workflow execution state could not be created or updated safely."""


class WorkflowExecutionStore(Protocol):
    def create(self, execution: WorkflowExecution) -> WorkflowExecution: ...
    def claim(self, execution: WorkflowExecution) -> WorkflowExecution | None: ...
    def get(self, owner_id: str, execution_id: str) -> WorkflowExecution | None: ...
    def list_for_owner(self, owner_id: str, limit: int) -> Sequence[WorkflowExecution]: ...
    def update(self, execution: WorkflowExecution) -> WorkflowExecution: ...


class SqliteWorkflowExecutionStore:
    """SQLite persistence with optimistic revisions for one local runtime."""

    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    def create(self, execution: WorkflowExecution) -> WorkflowExecution:
        if execution.created_at is None or execution.updated_at is None:
            timestamp = _now()
            execution = replace(execution, created_at=timestamp, updated_at=timestamp)
        try:
            with self._database.connect() as database:
                database.execute(
                    """
                    INSERT INTO workflow_executions (
                        execution_id, owner_id, workflow_id, workflow_path, workflow_version,
                        base_revision, invocation_arguments, next_step_id, status, step_results,
                        active_gate_id, gate_decision, proposal_id, committed_revision, error_code, revision,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    _values(execution),
                )
        except Exception as error:
            raise WorkflowExecutionStoreError("Workflow execution state could not be created.") from error
        created = self.get(execution.owner_id, execution.execution_id)
        assert created is not None
        return created

    def get(self, owner_id: str, execution_id: str) -> WorkflowExecution | None:
        try:
            with self._database.connect() as database:
                row = database.execute(
                    "SELECT * FROM workflow_executions WHERE owner_id = ? AND execution_id = ?",
                    (owner_id, execution_id),
                ).fetchone()
        except Exception as error:
            raise WorkflowExecutionStoreError("Workflow execution state could not be loaded.") from error
        return _from_row(row) if row is not None else None

    def claim(self, execution: WorkflowExecution) -> WorkflowExecution | None:
        """Atomically transfer one accepted execution to its sole local runner."""
        if execution.status != "accepted":
            raise WorkflowExecutionStoreError("Only accepted executions can be claimed.")
        timestamp = _now()
        try:
            with self._database.connect() as database:
                cursor = database.execute(
                    """
                    UPDATE workflow_executions
                    SET status = ?, updated_at = ?, revision = revision + 1
                    WHERE owner_id = ? AND execution_id = ? AND revision = ? AND status = ?
                    """,
                    (
                        "running", _timestamp(timestamp), execution.owner_id,
                        execution.execution_id, execution.revision, "accepted",
                    ),
                )
        except Exception as error:
            raise WorkflowExecutionStoreError("Workflow execution state could not be claimed.") from error
        if cursor.rowcount == 0:
            return None
        claimed = self.get(execution.owner_id, execution.execution_id)
        assert claimed is not None
        return claimed

    def list_for_owner(self, owner_id: str, limit: int) -> Sequence[WorkflowExecution]:
        if limit < 1:
            raise WorkflowExecutionStoreError("Workflow execution list limit must be positive.")
        try:
            with self._database.connect() as database:
                rows = database.execute(
                    "SELECT * FROM workflow_executions WHERE owner_id = ? "
                    "ORDER BY updated_at DESC, execution_id DESC LIMIT ?",
                    (owner_id, limit),
                ).fetchall()
        except Exception as error:
            raise WorkflowExecutionStoreError("Workflow execution state could not be listed.") from error
        return [_from_row(row) for row in rows]

    def update(self, execution: WorkflowExecution) -> WorkflowExecution:
        timestamp = _now()
        execution = replace(
            execution,
            created_at=execution.created_at or timestamp,
            updated_at=timestamp,
        )
        try:
            with self._database.connect() as database:
                cursor = database.execute(
                    """
                    UPDATE workflow_executions SET
                        next_step_id = ?, status = ?, step_results = ?, active_gate_id = ?,
                        gate_decision = ?, proposal_id = ?, committed_revision = ?, error_code = ?,
                        updated_at = ?, revision = revision + 1
                    WHERE owner_id = ? AND execution_id = ? AND revision = ?
                    """,
                    (
                        execution.next_step_id, execution.status, _json(execution.step_results),
                        execution.active_gate_id, execution.gate_decision, execution.proposal_id,
                        execution.committed_revision, execution.error_code, _timestamp(execution.updated_at), execution.owner_id,
                        execution.execution_id, execution.revision,
                    ),
                )
        except Exception as error:
            raise WorkflowExecutionStoreError("Workflow execution state could not be updated.") from error
        if cursor.rowcount != 1:
            raise WorkflowExecutionStoreError("Workflow execution state changed concurrently.")
        updated = self.get(execution.owner_id, execution.execution_id)
        assert updated is not None
        return updated


def _values(execution: WorkflowExecution) -> tuple[object, ...]:
    return (
        execution.execution_id, execution.owner_id, execution.workflow_id, execution.workflow_path,
        execution.workflow_version, execution.base_revision, _json(execution.invocation_arguments),
        execution.next_step_id, execution.status, _json(execution.step_results), execution.active_gate_id,
        execution.gate_decision, execution.proposal_id, execution.committed_revision, execution.error_code,
        _timestamp(execution.created_at), _timestamp(execution.updated_at),
    )


def _json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _from_row(row: object) -> WorkflowExecution:
    values = row
    return WorkflowExecution(
        execution_id=str(values["execution_id"]),
        owner_id=str(values["owner_id"]),
        workflow_id=str(values["workflow_id"]),
        workflow_path=str(values["workflow_path"]),
        workflow_version=str(values["workflow_version"]),
        base_revision=str(values["base_revision"]),
        invocation_arguments=dict(json.loads(values["invocation_arguments"])),
        next_step_id=values["next_step_id"],
        status=str(values["status"]),
        step_results=dict(json.loads(values["step_results"])),
        active_gate_id=values["active_gate_id"],
        gate_decision=values["gate_decision"],
        proposal_id=values["proposal_id"],
        committed_revision=values["committed_revision"],
        error_code=values["error_code"],
        revision=int(values["revision"]),
        created_at=datetime.fromisoformat(str(values["created_at"])),
        updated_at=datetime.fromisoformat(str(values["updated_at"])),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None) -> str:
    assert value is not None
    return value.isoformat(timespec="microseconds")
