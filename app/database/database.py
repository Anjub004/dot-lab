"""SQLAlchemy engine/session management.

V1 uses the synchronous SQLAlchemy API with SQLite. Database calls are short
and local, so calling them from async code is acceptable for a single-process
deployment. A production deployment would move to PostgreSQL and an async
driver (or run DB work in a thread pool).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import DateTime, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator


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
        """Create tables if they do not exist (V1 has no migrations)."""
        from app.database import models  # noqa: F401  (registers models)

        Base.metadata.create_all(self.engine)

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
