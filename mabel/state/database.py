"""Single-owner SQLite initialization for MABEL live state."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class DatabaseError(RuntimeError):
    """Raised when the local MABEL state database cannot be initialized."""


class SqliteDatabase:
    """A small versioned database boundary, intentionally without an ORM."""

    _SCHEMA_VERSION = 9

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def initialize(self) -> None:
        """Create or validate the database once during application startup."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                self._create_initial_schema(connection)
            elif version == 1:
                self._migrate_1_to_2(connection)
                self._migrate_2_to_3(connection)
                self._migrate_3_to_4(connection)
                self._migrate_4_to_5(connection)
                self._migrate_5_to_6(connection)
                self._migrate_6_to_7(connection)
                self._migrate_7_to_8(connection)
                self._migrate_8_to_9(connection)
            elif version == 2:
                self._migrate_2_to_3(connection)
                self._migrate_3_to_4(connection)
                self._migrate_4_to_5(connection)
                self._migrate_5_to_6(connection)
                self._migrate_6_to_7(connection)
                self._migrate_7_to_8(connection)
                self._migrate_8_to_9(connection)
            elif version == 3:
                self._migrate_3_to_4(connection)
                self._migrate_4_to_5(connection)
                self._migrate_5_to_6(connection)
                self._migrate_6_to_7(connection)
                self._migrate_7_to_8(connection)
                self._migrate_8_to_9(connection)
            elif version == 4:
                self._migrate_4_to_5(connection)
                self._migrate_5_to_6(connection)
                self._migrate_6_to_7(connection)
                self._migrate_7_to_8(connection)
                self._migrate_8_to_9(connection)
            elif version == 5:
                self._migrate_5_to_6(connection)
                self._migrate_6_to_7(connection)
                self._migrate_7_to_8(connection)
                self._migrate_8_to_9(connection)
            elif version == 6:
                self._migrate_6_to_7(connection)
                self._migrate_7_to_8(connection)
                self._migrate_8_to_9(connection)
            elif version == 7:
                self._migrate_7_to_8(connection)
                self._migrate_8_to_9(connection)
            elif version == 8:
                self._migrate_8_to_9(connection)
            elif version != self._SCHEMA_VERSION:
                raise DatabaseError(f"Unsupported MABEL state database version: {version}")
            else:
                return
            connection.execute(f"PRAGMA user_version = {self._SCHEMA_VERSION}")

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _create_initial_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE connections (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                connection_type TEXT NOT NULL,
                transport TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                credential_id TEXT,
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX connections_owner_id_idx ON connections (owner_id)"
        )
        SqliteDatabase._create_amie_connection_credential_table(connection)
        SqliteDatabase._create_managed_repository_tables(connection)
        SqliteDatabase._create_api_token_table(connection)
        SqliteDatabase._create_repository_projection_table(connection)
        SqliteDatabase._create_owner_identity_table(connection)
        SqliteDatabase._create_capability_control_table(connection)
        SqliteDatabase._create_workflow_execution_table(connection)

    @staticmethod
    def _migrate_1_to_2(connection: sqlite3.Connection) -> None:
        SqliteDatabase._create_managed_repository_tables(connection)

    @staticmethod
    def _migrate_2_to_3(connection: sqlite3.Connection) -> None:
        SqliteDatabase._create_api_token_table(connection)

    @staticmethod
    def _migrate_3_to_4(connection: sqlite3.Connection) -> None:
        managed_repositories = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'managed_repositories'"
        ).fetchone()
        if managed_repositories is not None:
            connection.execute(
                "UPDATE managed_repositories SET purpose = 'mabel_context_repository' "
                "WHERE purpose = 'capability_repository'"
            )
        SqliteDatabase._create_repository_projection_table(connection)

    @staticmethod
    def _migrate_4_to_5(connection: sqlite3.Connection) -> None:
        SqliteDatabase._create_owner_identity_table(connection)

    @staticmethod
    def _migrate_5_to_6(connection: sqlite3.Connection) -> None:
        SqliteDatabase._create_amie_connection_credential_table(connection)
        connections = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'connections'"
        ).fetchone()
        if connections is None:
            # This preserves the existing narrow version-2 migration behavior
            # for an otherwise empty historical database fixture.
            return
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(connections)").fetchall()
        }
        if "credential_id" not in columns:
            connection.execute("ALTER TABLE connections ADD COLUMN credential_id TEXT")
        # Versions through v5 accepted an arbitrary browser value into
        # credential_reference. Never use or retain that potentially plaintext
        # secret once secure AMIE bearer storage is available.
        if "credential_reference" in columns:
            connection.execute("UPDATE connections SET credential_reference = NULL")

    @staticmethod
    def _migrate_6_to_7(connection: sqlite3.Connection) -> None:
        SqliteDatabase._create_capability_control_table(connection)

    @staticmethod
    def _migrate_7_to_8(connection: sqlite3.Connection) -> None:
        SqliteDatabase._create_workflow_execution_table(connection)

    @staticmethod
    def _migrate_8_to_9(connection: sqlite3.Connection) -> None:
        """Add operational timestamps without fabricating historic event time."""
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(workflow_executions)").fetchall()
        }
        temporary_default = "1970-01-01T00:00:00.000000+00:00"
        if "created_at" not in columns:
            connection.execute(
                "ALTER TABLE workflow_executions ADD COLUMN created_at TEXT NOT NULL "
                f"DEFAULT '{temporary_default}'"
            )
        if "updated_at" not in columns:
            connection.execute(
                "ALTER TABLE workflow_executions ADD COLUMN updated_at TEXT NOT NULL "
                f"DEFAULT '{temporary_default}'"
            )
        timestamp = _timestamp()
        connection.execute(
            "UPDATE workflow_executions SET created_at = ?, updated_at = ? "
            "WHERE created_at = ? OR updated_at = ?",
            (timestamp, timestamp, temporary_default, temporary_default),
        )
        SqliteDatabase._create_workflow_execution_indexes(connection)

    @staticmethod
    def _create_workflow_execution_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS workflow_executions (
                execution_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                workflow_path TEXT NOT NULL,
                workflow_version TEXT NOT NULL,
                base_revision TEXT NOT NULL,
                invocation_arguments TEXT NOT NULL,
                next_step_id TEXT,
                status TEXT NOT NULL,
                step_results TEXT NOT NULL,
                active_gate_id TEXT,
                gate_decision TEXT,
                proposal_id TEXT,
                committed_revision TEXT,
                error_code TEXT,
                revision INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        SqliteDatabase._create_workflow_execution_indexes(connection)

    @staticmethod
    def _create_workflow_execution_indexes(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE INDEX IF NOT EXISTS workflow_executions_owner_id_idx "
            "ON workflow_executions (owner_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS workflow_executions_owner_updated_execution_idx "
            "ON workflow_executions (owner_id, updated_at DESC, execution_id DESC)"
        )

    @staticmethod
    def _create_capability_control_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS capability_controls (
                owner_id TEXT NOT NULL,
                capability_id TEXT NOT NULL,
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                validation_state TEXT NOT NULL,
                validation_error_code TEXT,
                last_validated_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (owner_id, capability_id)
            )
            """
        )

    @staticmethod
    def _create_amie_connection_credential_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS amie_connection_credentials (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                encrypted_token TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def _create_owner_identity_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE owner_identities (
                provider TEXT NOT NULL,
                subject TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (provider, subject),
                UNIQUE (provider, owner_id)
            )
            """
        )

    @staticmethod
    def _create_repository_projection_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE repository_projections (
                repository_id TEXT PRIMARY KEY,
                commit_sha TEXT NOT NULL,
                synced_at TEXT NOT NULL,
                status TEXT NOT NULL,
                error_code TEXT,
                FOREIGN KEY (repository_id) REFERENCES managed_repositories(id)
            )
            """
        )

    @staticmethod
    def _create_api_token_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE api_tokens (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL UNIQUE,
                token_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def _create_managed_repository_tables(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE managed_repository_credentials (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                encrypted_secret TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE managed_repositories (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                purpose TEXT NOT NULL,
                display_name TEXT NOT NULL,
                provider_repository_id TEXT NOT NULL,
                canonical_locator TEXT NOT NULL,
                default_branch TEXT NOT NULL,
                credential_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (owner_id, provider, provider_repository_id),
                FOREIGN KEY (credential_id) REFERENCES managed_repository_credentials(id)
            )
            """
        )
        connection.execute(
            "CREATE INDEX managed_repositories_owner_id_idx ON managed_repositories (owner_id)"
        )
        connection.execute(
            "CREATE INDEX managed_repositories_owner_purpose_idx "
            "ON managed_repositories (owner_id, purpose)"
        )


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
