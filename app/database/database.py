"""SQLAlchemy engine/session management and schema migrations.

V1 uses the synchronous SQLAlchemy API with SQLite. Database calls are short
and local, so calling them from async code is acceptable for a single-process
deployment. A production deployment would move to PostgreSQL and an async
driver (or run DB work in a thread pool).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import DateTime, create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
# Databases created before migrations existed (Milestones 1-3, via create_all)
# have no alembic_version table. They are brought up to this baseline revision.
BASELINE_REVISION = "0001"
BASELINE_TABLES = (
    "monitors",
    "notifications",
    "tasks",
    "approvals",
    "llm_usage",
    "memories",
    "task_events",
    "tool_calls",
)


class DatabaseError(RuntimeError):
    """Raised when a database operation fails."""


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def utcnow() -> datetime:
    """Timezone-aware current UTC time."""
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Stores datetimes as UTC and always returns timezone-aware values.

    SQLite has no native timezone support, so values come back naive; this
    decorator re-attaches UTC on the way out.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


def _ensure_sqlite_directory(url: str) -> None:
    prefix = "sqlite:///"
    if url.startswith(prefix) and ":memory:" not in url:
        Path(url[len(prefix) :]).parent.mkdir(parents=True, exist_ok=True)


class Database:
    """Owns the engine and hands out transactional sessions."""

    def __init__(self, url: str) -> None:
        self.url = url
        is_sqlite = url.startswith("sqlite")
        if is_sqlite:
            _ensure_sqlite_directory(url)
        connect_args = {"check_same_thread": False, "timeout": 30} if is_sqlite else {}
        self.engine: Engine = create_engine(url, connect_args=connect_args, future=True)
        if is_sqlite:
            event.listen(self.engine, "connect", _configure_sqlite)
        self._sessions = sessionmaker(bind=self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        """Create the current schema directly from the models (used by unit tests)."""
        from app.database import models  # noqa: F401  (registers models)

        Base.metadata.create_all(self.engine)

    def migrate(self) -> None:
        """Bring the database schema to the latest Alembic revision.

        * New database: every migration runs from scratch.
        * Database from before migrations existed: missing baseline tables are
          created, the baseline is recorded, then newer migrations run.
        * Up-to-date database: nothing happens.

        SQLite cannot alter tables in place, so Alembic rebuilds them (copy,
        drop, rename). Foreign keys are switched off while that happens, because
        dropping a parent table with foreign keys on would cascade-delete rows in
        child tables (task events, tool calls, ...).
        """
        from alembic import command
        from alembic.config import Config

        from app.database import models  # noqa: F401  (registers models)

        config = Config()
        config.set_main_option("script_location", str(MIGRATIONS_DIR))
        config.set_main_option("sqlalchemy.url", self.url)
        is_sqlite = self.url.startswith("sqlite")

        with self.engine.connect() as conn:
            if is_sqlite:
                # Must run before any transaction starts, or SQLite ignores it.
                conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
            try:
                config.attributes["connection"] = conn
                tables = set(inspect(conn).get_table_names())
                if "alembic_version" not in tables and "tasks" in tables:
                    legacy = [Base.metadata.tables[name] for name in BASELINE_TABLES]
                    Base.metadata.create_all(conn, tables=legacy, checkfirst=True)
                    conn.commit()
                    command.stamp(config, BASELINE_REVISION)
                    logger.info("existing database marked as migration baseline")
                command.upgrade(config, "head")
                conn.commit()
                if is_sqlite:
                    problems = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
                    if problems:
                        logger.warning("foreign key check found %s problem(s)", len(problems))
            finally:
                if is_sqlite:
                    conn.exec_driver_sql("PRAGMA foreign_keys=ON")

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Transactional scope: commit on success, roll back on error."""
        session = self._sessions()
        try:
            yield session
            session.commit()
        except SQLAlchemyError as exc:
            session.rollback()
            raise DatabaseError(f"Database operation failed: {type(exc).__name__}") from exc
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()


def _configure_sqlite(dbapi_connection: object, _record: object) -> None:
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()
