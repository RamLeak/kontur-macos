"""SQLAlchemy engine/session setup and schema bootstrap."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .schema_version import CURRENT_SCHEMA_VERSION


class Base(DeclarativeBase):
    """Base for all backend models."""


class SchemaVersionError(RuntimeError):
    """The database was written by a newer backend than this process."""


def make_engine(database_url: str) -> Engine:
    """Create an SQLite-first engine with defensive connection pragmas."""

    connect_args: dict[str, Any] = {"check_same_thread": False}
    kwargs: dict[str, Any] = {"future": True, "pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        if ":memory:" in database_url:
            kwargs["poolclass"] = StaticPool
        else:
            # Parent directories are created only for the configured DB path; no
            # broad cleanup or external filesystem probing occurs here.
            # Directory creation is deferred to ``init_schema`` so importing
            # the ASGI module stays side-effect-light.
            pass
        connect_args["timeout"] = 10.0

    engine = create_engine(database_url, connect_args=connect_args, **kwargs)

    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=10000")
            if ":memory:" not in database_url:
                cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


def init_schema(engine: Engine) -> None:
    """Bootstrap the current metadata and record a monotonic schema version.

    Alembic can replace this bootstrap in a later multi-node migration, but local
    development must remain usable from a clean checkout without external tools.
    """

    if str(engine.url).startswith("sqlite") and ":memory:" not in str(engine.url):
        # The rendered URL escapes Windows drive colons (C%3A). The database
        # component retains the actual local path used by the SQLite driver.
        raw_path = engine.url.database
        if raw_path and raw_path != ":memory:":
            Path(raw_path).expanduser().parent.mkdir(parents=True, exist_ok=True)

    # Import models before applying the ordered migration registry so metadata
    # is complete. Version 1 intentionally uses SQLAlchemy's deterministic
    # create-all bootstrap; version 2 adds the immutable context/error fields
    # required by the assistant idempotency and learning DTO contracts.
    from . import models  # noqa: F401

    with engine.begin() as connection:
        # Python sqlite legacy transaction control does not begin for DDL.
        # Make the entire ordered schema transition truly atomic before the
        # first CREATE/ALTER/TRIGGER, including the migration marker.
        if engine.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        connection.execute(
            text("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        )
        existing = connection.execute(text("SELECT MAX(version) FROM schema_migrations")).scalar()
        current = int(existing) if existing is not None else 0
        if current > CURRENT_SCHEMA_VERSION:
            raise SchemaVersionError(
                f"database schema version {current} is newer than supported {CURRENT_SCHEMA_VERSION}"
            )
        if current < 1:
            Base.metadata.create_all(connection)
            connection.execute(text("INSERT INTO schema_migrations(version, applied_at) VALUES (1, CURRENT_TIMESTAMP)"))
            current = 1

        # ``create_all`` is intentionally idempotent for an existing v1
        # database; it also makes test-created partial databases recoverable.
        # It does not add columns to an existing table, so the ordered v2
        # migration below checks each column before issuing SQLite ALTERs.
        Base.metadata.create_all(connection)
        if current < 2:
            _apply_v2(connection)
            connection.execute(text("INSERT INTO schema_migrations(version, applied_at) VALUES (2, CURRENT_TIMESTAMP)"))
            current = 2
        if current < 3:
            _apply_v3(connection)
            connection.execute(text("INSERT INTO schema_migrations(version, applied_at) VALUES (3, CURRENT_TIMESTAMP)"))
            current = 3
        if current < 4:
            _apply_v4(connection)
            connection.execute(text("INSERT INTO schema_migrations(version, applied_at) VALUES (4, CURRENT_TIMESTAMP)"))
            current = 4
        if current < 5:
            _apply_v5(connection)
            connection.execute(text("INSERT INTO schema_migrations(version, applied_at) VALUES (5, CURRENT_TIMESTAMP)"))
            current = 5
        if current < 6:
            _add_column_if_missing(connection, "exam_sessions", "paused_at DATETIME")
            _add_column_if_missing(connection, "exam_sessions", "paused_seconds FLOAT NOT NULL DEFAULT 0")
            connection.execute(text("INSERT INTO schema_migrations(version, applied_at) VALUES (6, CURRENT_TIMESTAMP)"))
            current = 6
        if current < 7:
            _apply_v7(connection)
            connection.execute(text("INSERT INTO schema_migrations(version, applied_at) VALUES (7, CURRENT_TIMESTAMP)"))
            current = 7
        if current < 8:
            _apply_v8(connection)
            connection.execute(text("INSERT INTO schema_migrations(version, applied_at) VALUES (8, CURRENT_TIMESTAMP)"))
            current = 8
        if current < 9:
            _apply_v9(connection)
            connection.execute(text("INSERT INTO schema_migrations(version, applied_at) VALUES (9, CURRENT_TIMESTAMP)"))
            current = 9
        if current < 10:
            _apply_v10(connection)
            connection.execute(
                text("INSERT INTO schema_migrations(version, applied_at) VALUES (10, CURRENT_TIMESTAMP)")
            )
            current = 10
        if current < 11:
            _apply_v11(connection)
            connection.execute(
                text("INSERT INTO schema_migrations(version, applied_at) VALUES (11, CURRENT_TIMESTAMP)")
            )
            current = 11
        if current < 12:
            _apply_v12(connection)
            connection.execute(
                text("INSERT INTO schema_migrations(version, applied_at) VALUES (12, CURRENT_TIMESTAMP)")
            )
            current = 12

        if current < 13:
            from .sync_records import install_change_triggers

            install_change_triggers(connection)
            connection.execute(
                text("INSERT INTO schema_migrations(version, applied_at) VALUES (13, CURRENT_TIMESTAMP)")
            )
            current = 13

        # Keep a final idempotent metadata pass for clean databases and for
        # partial test schemas restored before the migration registry existed.
        Base.metadata.create_all(connection)


def _table_columns(connection: Any, table: str) -> set[str]:
    rows = connection.exec_driver_sql(f"PRAGMA table_info('{table}')").fetchall()
    return {str(row[1]) for row in rows}


def _add_column_if_missing(connection: Any, table: str, definition: str) -> None:
    column = definition.split()[0]
    if column not in _table_columns(connection, table):
        connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {definition}")


def _apply_v2(connection: Any) -> None:
    """Apply the additive v2 contract without an Alembic dependency."""

    _add_column_if_missing(connection, "context_packs", "rendered_sections JSON NOT NULL DEFAULT '[]'")
    _add_column_if_missing(connection, "error_events", "reason TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(connection, "error_events", "task_line INTEGER")
    _add_column_if_missing(
        connection,
        "error_events",
        "policy_version VARCHAR(32) NOT NULL DEFAULT 'error-capture-v1'",
    )
    _add_column_if_missing(connection, "review_items", "reason TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(connection, "review_items", "task_line INTEGER")
    _add_column_if_missing(
        connection,
        "review_items",
        "policy_version VARCHAR(32) NOT NULL DEFAULT 'mastery-v1'",
    )


def _apply_v3(connection: Any) -> None:
    """Create the owner-scoped assistant idempotency reservation table."""

    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS idempotency_reservations (
            id VARCHAR(36) NOT NULL,
            owner_id VARCHAR(36) NOT NULL,
            idempotency_key VARCHAR(255) NOT NULL,
            request_hash VARCHAR(64) NOT NULL,
            conversation_id VARCHAR(36) NOT NULL,
            message_hash VARCHAR(64) NOT NULL,
            message_id VARCHAR(36),
            context_pack_id VARCHAR(36),
            agent_run_id VARCHAR(36),
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT uq_idempotency_owner_key UNIQUE (owner_id, idempotency_key),
            FOREIGN KEY (owner_id) REFERENCES users (id),
            FOREIGN KEY (conversation_id) REFERENCES conversations (id),
            FOREIGN KEY (message_id) REFERENCES messages (id),
            FOREIGN KEY (context_pack_id) REFERENCES context_packs (id),
            FOREIGN KEY (agent_run_id) REFERENCES agent_runs (id)
        )
        """
    )
    for column in ("owner_id", "conversation_id", "message_id", "context_pack_id", "agent_run_id"):
        connection.exec_driver_sql(
            f"CREATE INDEX IF NOT EXISTS ix_idempotency_reservations_{column} ON idempotency_reservations ({column})"
        )


def _apply_v4(connection: Any) -> None:
    """Add durable source checkpoints and lazy asset fetch state."""

    _add_column_if_missing(connection, "sync_runs", "owner_id VARCHAR(64)")
    _add_column_if_missing(connection, "sync_runs", "request_hash VARCHAR(64)")
    _add_column_if_missing(connection, "assets", "fetch_attempts INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(connection, "assets", "fetch_error TEXT")
    _add_column_if_missing(connection, "assets", "fetch_started_at DATETIME")
    _add_column_if_missing(connection, "assets", "fetched_at DATETIME")
    _add_column_if_missing(connection, "assets", "hash_verified BOOLEAN NOT NULL DEFAULT 0")
    _add_column_if_missing(connection, "assets", "expected_content_hash VARCHAR(64)")
    _add_column_if_missing(connection, "task_revisions", "subtask_manifest JSON NOT NULL DEFAULT '[]'")
    _add_column_if_missing(connection, "task_revisions", "table_manifest JSON")
    _add_column_if_missing(connection, "task_revisions", "target_exam_year INTEGER")

    # New tables are created by the metadata pass above. The explicit indexes
    # keep the worker/API cursor and owner lookups bounded on upgraded DBs.
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_sync_runs_owner_created ON sync_runs (owner_id, created_at, id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_source_sync_items_job_status_ordinal "
        "ON source_sync_items (job_id, status, ordinal)"
    )


def _apply_v5(connection: Any) -> None:
    """Add source metadata without relabelling any existing revision."""

    for definition in (
        "source_year INTEGER",
        "source_year_basis VARCHAR(64)",
        "source_created_at DATETIME",
        "source_difficulty INTEGER",
        "difficulty VARCHAR(16)",
        "source_label TEXT",
        "source_metadata JSON NOT NULL DEFAULT '{}'",
    ):
        _add_column_if_missing(connection, "task_revisions", definition)
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_task_revisions_source_year_difficulty_created "
        "ON task_revisions (source_year, difficulty, source_created_at)"
    )


def _apply_v7(connection: Any) -> None:
    """Create durable owner-scoped task bookmarks without rewriting old rows."""

    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS bookmarks (
            id VARCHAR(36) NOT NULL,
            owner_id VARCHAR(64) NOT NULL,
            task_id VARCHAR(36) NOT NULL,
            task_revision_id VARCHAR(36) NOT NULL,
            created_at DATETIME NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT uq_bookmark_owner_task UNIQUE (owner_id, task_id),
            FOREIGN KEY (owner_id) REFERENCES users (id),
            FOREIGN KEY (task_id) REFERENCES tasks (id),
            FOREIGN KEY (task_revision_id) REFERENCES task_revisions (id)
        )
        """
    )
    for column in ("owner_id", "task_id", "task_revision_id"):
        connection.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS ix_bookmarks_{column} ON bookmarks ({column})")


def _apply_v8(connection: Any) -> None:
    """Add explicit exam modes and independent item workspaces additively."""

    # Rows created before strict/study was introduced retain the old sealed
    # behavior and are intentionally labelled ``legacy`` for the UI.
    _add_column_if_missing(connection, "exam_sessions", "mode VARCHAR(16) NOT NULL DEFAULT 'legacy'")
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS exam_workspaces (
            id VARCHAR(36) NOT NULL,
            owner_id VARCHAR(64) NOT NULL,
            exam_session_id VARCHAR(36) NOT NULL,
            exam_item_id VARCHAR(36) NOT NULL,
            code_content TEXT NOT NULL DEFAULT '',
            code_version INTEGER NOT NULL DEFAULT 0,
            board_snapshot JSON NOT NULL DEFAULT '{}',
            board_version INTEGER NOT NULL DEFAULT 0,
            updated_at DATETIME NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT uq_exam_workspace_item UNIQUE (exam_session_id, exam_item_id),
            FOREIGN KEY (owner_id) REFERENCES users (id),
            FOREIGN KEY (exam_session_id) REFERENCES exam_sessions (id),
            FOREIGN KEY (exam_item_id) REFERENCES exam_items (id)
        )
        """
    )
    for column in ("owner_id", "exam_session_id", "exam_item_id"):
        connection.exec_driver_sql(
            f"CREATE INDEX IF NOT EXISTS ix_exam_workspaces_{column} ON exam_workspaces ({column})"
        )


def _apply_v9(connection: Any) -> None:
    """Add immutable P4 preparation metadata without rewriting exam rows."""

    _add_column_if_missing(connection, "exam_sessions", "prepare_kind VARCHAR(16)")
    _add_column_if_missing(connection, "exam_sessions", "prepare_options JSON")
    _add_column_if_missing(connection, "exam_sessions", "prepared_at DATETIME")
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_exam_sessions_owner_lifecycle_started "
        "ON exam_sessions (owner_id, lifecycle, started_at, id)"
    )


def _apply_v10(connection: Any) -> None:
    """Store immutable outputs for attested exam code runs."""

    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS exam_code_runs (
            id VARCHAR(36) NOT NULL,
            owner_id VARCHAR(64) NOT NULL,
            exam_session_id VARCHAR(36) NOT NULL,
            exam_item_id VARCHAR(36) NOT NULL,
            task_revision_id VARCHAR(36) NOT NULL,
            workspace_id VARCHAR(36) NOT NULL,
            workspace_version INTEGER NOT NULL,
            code_content TEXT NOT NULL,
            code_hash VARCHAR(64) NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'queued',
            executor_status VARCHAR(64) NOT NULL DEFAULT 'unavailable',
            stdout TEXT NOT NULL DEFAULT '',
            stderr TEXT NOT NULL DEFAULT '',
            exit_code INTEGER,
            timed_out BOOLEAN NOT NULL DEFAULT 0,
            duration_ms INTEGER,
            resource_json JSON NOT NULL DEFAULT '{}',
            input_manifest JSON NOT NULL DEFAULT '{}',
            input_digest VARCHAR(64),
            idempotency_key VARCHAR(255) NOT NULL,
            request_hash VARCHAR(64) NOT NULL,
            created_at DATETIME NOT NULL,
            completed_at DATETIME,
            PRIMARY KEY (id),
            CONSTRAINT uq_exam_code_run_owner_key UNIQUE (owner_id, idempotency_key),
            FOREIGN KEY (owner_id) REFERENCES users (id),
            FOREIGN KEY (exam_session_id) REFERENCES exam_sessions (id),
            FOREIGN KEY (exam_item_id) REFERENCES exam_items (id),
            FOREIGN KEY (task_revision_id) REFERENCES task_revisions (id),
            FOREIGN KEY (workspace_id) REFERENCES exam_workspaces (id)
        )
        """
    )
    for column in ("owner_id", "exam_session_id", "exam_item_id", "task_revision_id", "workspace_id"):
        connection.exec_driver_sql(
            f"CREATE INDEX IF NOT EXISTS ix_exam_code_runs_{column} ON exam_code_runs ({column})"
        )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_exam_code_runs_item_created "
        "ON exam_code_runs (owner_id, exam_session_id, exam_item_id, created_at, id)"
    )


def _apply_v11(connection: Any) -> None:
    """Persist immutable, exam-scoped Study AI snapshots and job links."""

    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS exam_study_ai_context_snapshots (
            id VARCHAR(36) NOT NULL,
            owner_id VARCHAR(64) NOT NULL,
            exam_session_id VARCHAR(36) NOT NULL,
            exam_item_id VARCHAR(36) NOT NULL,
            task_revision_id VARCHAR(36) NOT NULL,
            workspace_version INTEGER NOT NULL,
            snapshot_json JSON NOT NULL,
            snapshot_hash VARCHAR(64) NOT NULL,
            created_at DATETIME NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY (owner_id) REFERENCES users (id),
            FOREIGN KEY (exam_session_id) REFERENCES exam_sessions (id),
            FOREIGN KEY (exam_item_id) REFERENCES exam_items (id),
            FOREIGN KEY (task_revision_id) REFERENCES task_revisions (id)
        )
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS exam_study_ai_runs (
            id VARCHAR(36) NOT NULL,
            owner_id VARCHAR(64) NOT NULL,
            exam_session_id VARCHAR(36) NOT NULL,
            exam_item_id VARCHAR(36) NOT NULL,
            task_revision_id VARCHAR(36) NOT NULL,
            workspace_version INTEGER NOT NULL,
            context_snapshot_id VARCHAR(36) NOT NULL,
            agent_run_id VARCHAR(36) NOT NULL,
            message_hash VARCHAR(64) NOT NULL,
            request_hash VARCHAR(64) NOT NULL,
            idempotency_key VARCHAR(255),
            created_at DATETIME NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT uq_exam_study_ai_owner_key UNIQUE (owner_id, idempotency_key),
            CONSTRAINT uq_exam_study_ai_agent_run UNIQUE (agent_run_id),
            FOREIGN KEY (owner_id) REFERENCES users (id),
            FOREIGN KEY (exam_session_id) REFERENCES exam_sessions (id),
            FOREIGN KEY (exam_item_id) REFERENCES exam_items (id),
            FOREIGN KEY (task_revision_id) REFERENCES task_revisions (id),
            FOREIGN KEY (context_snapshot_id) REFERENCES exam_study_ai_context_snapshots (id),
            FOREIGN KEY (agent_run_id) REFERENCES agent_runs (id)
        )
        """
    )
    for table, columns in (
        ("exam_study_ai_context_snapshots", ("owner_id", "exam_session_id", "exam_item_id", "task_revision_id")),
        (
            "exam_study_ai_runs",
            (
                "owner_id",
                "exam_session_id",
                "exam_item_id",
                "task_revision_id",
                "context_snapshot_id",
                "agent_run_id",
            ),
        ),
    ):
        for column in columns:
            connection.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS ix_{table}_{column} ON {table} ({column})")


def _apply_v12(connection: Any) -> None:
    """Link owner errors to immutable exam items and source revisions."""

    _add_column_if_missing(
        connection,
        "error_events",
        "task_revision_id VARCHAR(36) REFERENCES task_revisions(id)",
    )
    _add_column_if_missing(
        connection,
        "error_events",
        "exam_item_id VARCHAR(36) REFERENCES exam_items(id)",
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_error_events_task_revision_id "
        "ON error_events (task_revision_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_error_events_exam_item_id "
        "ON error_events (exam_item_id)"
    )


def session_scope(factory: sessionmaker[Session]) -> Generator[Session, None, None]:
    session = factory()
    try:
        yield session
    finally:
        session.close()
