from dataclasses import replace
from datetime import datetime, timedelta, timezone
import sqlite3
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from mabel.app import create_app
from mabel.config import Settings
from mabel.services.workflow_operations import (
    WorkflowExecutionNotFoundError,
    WorkflowExecutionOperationsError,
    WorkflowExecutionOperationsService,
)
from mabel.state.database import SqliteDatabase
from mabel.state.workflow_executions import SqliteWorkflowExecutionStore
from mabel.workflows.models import WorkflowExecution
from mabel.workflows.reporting import (
    serialize_workflow_execution_detail,
    serialize_workflow_execution_list_item,
)


BASE = "a" * 40


def _execution(owner_id="owner-a", **changes) -> WorkflowExecution:
    values = {
        "execution_id": str(uuid4()),
        "owner_id": owner_id,
        "workflow_id": "manga_flow",
        "workflow_path": "workflows/manga-flow.md",
        "workflow_version": "1.0",
        "base_revision": BASE,
        "invocation_arguments": {"token": "never-expose", "prompt": "never-expose"},
        "next_step_id": "write_file",
        "status": "running",
        "step_results": {},
    }
    values.update(changes)
    return WorkflowExecution(**values)


def _store(tmp_path):
    database = SqliteDatabase(tmp_path / "state.db")
    database.initialize()
    return database, SqliteWorkflowExecutionStore(database)


def test_v8_migration_adds_and_backfills_workflow_execution_timestamps(tmp_path) -> None:
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as database:
        database.execute(
            """
            CREATE TABLE workflow_executions (
                execution_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
                workflow_path TEXT NOT NULL, workflow_version TEXT NOT NULL, base_revision TEXT NOT NULL,
                invocation_arguments TEXT NOT NULL, next_step_id TEXT, status TEXT NOT NULL,
                step_results TEXT NOT NULL, active_gate_id TEXT, gate_decision TEXT, proposal_id TEXT,
                committed_revision TEXT, error_code TEXT, revision INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        database.execute(
            "INSERT INTO workflow_executions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid4()), "owner-a", "manga_flow", "workflows/manga-flow.md", "1.0", BASE, "{}", None,
             "completed", "{}", None, None, None, None, None, 0),
        )
        database.execute("PRAGMA user_version = 8")

    SqliteDatabase(path).initialize()

    with sqlite3.connect(path) as database:
        columns = {row[1] for row in database.execute("PRAGMA table_info(workflow_executions)")}
        created_at, updated_at = database.execute(
            "SELECT created_at, updated_at FROM workflow_executions"
        ).fetchone()
        index_names = {row[1] for row in database.execute("PRAGMA index_list(workflow_executions)")}
        version = database.execute("PRAGMA user_version").fetchone()[0]
    assert {"created_at", "updated_at"} <= columns
    assert created_at == updated_at
    assert datetime.fromisoformat(created_at).tzinfo == timezone.utc
    assert "workflow_executions_owner_updated_execution_idx" in index_names
    assert version == 9


def test_store_assigns_and_refreshes_timestamps_without_changing_created_at(tmp_path, monkeypatch) -> None:
    _, store = _store(tmp_path)
    first = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
    second = first + timedelta(seconds=1)
    moments = iter((first, second))
    monkeypatch.setattr("mabel.state.workflow_executions._now", lambda: next(moments))

    created = store.create(_execution())
    updated = store.update(replace(created, status="failed", error_code="handler_failed"))

    assert created.created_at == created.updated_at == first
    assert updated.created_at == first
    assert updated.updated_at == second
    assert updated.revision == 1


def test_operations_list_is_owner_scoped_sorted_and_bounded(tmp_path) -> None:
    _, store = _store(tmp_path)
    now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
    older = store.create(_execution(created_at=now, updated_at=now))
    newer = store.create(_execution(created_at=now, updated_at=now + timedelta(seconds=2)))
    tied_low = store.create(_execution(created_at=now, updated_at=now + timedelta(seconds=3), execution_id="00000000-0000-4000-8000-000000000001"))
    tied_high = store.create(_execution(created_at=now, updated_at=now + timedelta(seconds=3), execution_id="00000000-0000-4000-8000-000000000002"))
    store.create(_execution("owner-b", created_at=now, updated_at=now + timedelta(seconds=4)))
    service = WorkflowExecutionOperationsService(store)

    result = service.list("owner-a", limit=2)

    assert [item.execution_id for item in result.executions] == [tied_high.execution_id, tied_low.execution_id]
    assert result.has_more is True
    assert {item.execution_id for item in service.list("owner-a", limit=100).executions} == {
        older.execution_id, newer.execution_id, tied_low.execution_id, tied_high.execution_id,
    }
    assert service.list("missing").executions == []
    with pytest.raises(WorkflowExecutionOperationsError, match="invalid_execution_limit"):
        service.list("owner-a", limit=101)
    with pytest.raises(WorkflowExecutionNotFoundError):
        service.get("owner-a", str(uuid4()))


def test_reporting_serializer_is_deny_by_default_and_keeps_generic_step_identity(tmp_path) -> None:
    _, store = _store(tmp_path)
    execution = store.create(_execution(
        status="failed",
        error_code="handler_failed",
        step_results={
            "invoke_governed_agent": {
                "outcome": "failed",
                "evidence": {
                    "reason": "file_write_failed",
                    "changed_files": ["manga/chapter.py", "../secret.txt", "/workspace/secret"],
                    "validation": {"provider": "codex_cli", "elapsed_ms": 23, "exit_code": 1,
                                   "final_message": "secret", "endpoint": "https://private.example"},
                    "prompt": "secret", "workspace_id": "secret", "blockers": ["secret"],
                },
            },
            "unreviewed_action": {
                "outcome": "success",
                "evidence": {"reason": "file_write_failed", "token": "secret"},
            },
        },
    ))

    list_item = serialize_workflow_execution_list_item(execution)
    detail = serialize_workflow_execution_detail(execution)
    serialized = repr(detail)

    assert set(list_item) == {
        "execution_id", "workflow", "status", "created_at", "updated_at", "next_step_id",
        "active_gate_id", "requires_attention", "failure_code",
    }
    assert detail["workflow"]["id"] == "manga_flow"
    assert detail["failure"] == {"code": "handler_failed"}
    governed_agent = next(
        step for step in detail["progression"]["completed_steps"] if step["step_id"] == "invoke_governed_agent"
    )
    assert governed_agent["evidence"] == {
        "reason_code": "file_write_failed", "changed_files": ["manga/chapter.py"],
        "validation": {"provider": "codex", "elapsed_ms": 23, "exit_code": 1},
    }
    unreviewed = next(step for step in detail["progression"]["completed_steps"] if step["step_id"] == "unreviewed_action")
    assert unreviewed == {"step_id": "unreviewed_action", "outcome": "success"}
    for prohibited in ("never-expose", "secret", "prompt", "workspace_id", "blockers", "endpoint"):
        assert prohibited not in serialized


def test_reporting_represents_accepted_as_nonterminal_in_progress(tmp_path) -> None:
    _, store = _store(tmp_path)
    execution = store.create(_execution(status="accepted"))

    listed = serialize_workflow_execution_list_item(execution)
    detail = serialize_workflow_execution_detail(execution)

    assert listed["status"] == "accepted"
    assert listed["requires_attention"] is False
    assert detail["progression"]["terminal_status"] is None


async def test_workflow_operations_api_is_owner_scoped_and_bounded(tmp_path) -> None:
    settings = Settings(state_database_path=tmp_path / "state.db", development_owner_id="test-owner")
    app = create_app(settings=settings)
    async with app.router.lifespan_context(app):
        store = SqliteWorkflowExecutionStore(app.state.database)
        owned = store.create(_execution("test-owner", active_gate_id="operator_review", status="stopped"))
        other = store.create(_execution("other-owner"))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            listed = await client.get("/api/portal/workflow-executions?owner_id=other-owner")
            detail = await client.get(f"/api/portal/workflow-executions/{owned.execution_id}")
            cross_owner = await client.get(f"/api/portal/workflow-executions/{other.execution_id}")
            missing = await client.get(f"/api/portal/workflow-executions/{uuid4()}")
            invalid = await client.get("/api/portal/workflow-executions?limit=101")

    assert [item["execution_id"] for item in listed.json()["executions"]] == [owned.execution_id]
    assert detail.status_code == 200
    assert detail.json()["active_gate_id"] == "operator_review"
    assert cross_owner.status_code == missing.status_code == 404
    assert cross_owner.json() == missing.json()
    assert invalid.status_code == 422
