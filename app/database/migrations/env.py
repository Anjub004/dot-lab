"""Alembic environment for Dot Lab.

The database URL comes from the Alembic config (set by ``app.database.migrate``)
or, when run from the command line, from ``DATABASE_URL`` / ``.env`` via
:class:`app.config.Settings`. ``render_as_batch`` lets Alembic alter SQLite
tables (SQLite cannot drop or change columns in place).
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.database import models  # noqa: F401  (registers every table on Base.metadata)
from app.database.database import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    from app.config import Settings

    return Settings.from_env().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:  # called from app.database.migrate with an open connection
        _run(connection)
        return
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _url()
    engine = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with engine.connect() as conn:
        _run(conn)


def _run(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
