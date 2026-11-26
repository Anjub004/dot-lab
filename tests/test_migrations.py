"""Database migration tests: fresh databases, legacy databases and data safety."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from app.database import models  # noqa: F401
from app.database.database import MIGRATIONS_DIR, Base, Database


def _config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _head() -> str:
    return ScriptDirectory.from_config(_config("sqlite://")).get_current_head() or ""


def _revision(db: Database) -> str | None:
    with db.engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def test_fresh_database_matches_models(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'fresh.db'}")
    db.migrate()
    assert _revision(db) == _head()
    with db.engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], f"migrations and models differ: {diff}"
    db.dispose()


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'twice.db'}")
    db.migrate()
    db.migrate()  # e.g. a restart: nothing to do, no error
    assert _revision(db) == _head()
    db.dispose()


def _legacy_milestone1_database(path: Path) -> Database:
    """A database as Milestone 1 created it: no alembic_version, no llm_usage,
    no memory categories, no conversations. Filled with linked rows."""
    url = f"sqlite:///{path}"
    db = Database(url)
    with db.engine.begin() as conn:
        cfg = _config(url)
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0001")
        conn.exec_driver_sql("DROP TABLE llm_usage")
        conn.exec_driver_sql("DROP TABLE alembic_version")
        conn.execute(
            text(
                "INSERT INTO tasks (id, goal, status, iterations, created_at, updated_at) "
                "VALUES ('t1', 'old goal', 'COMPLETED', 3, '2026-11-01', '2026-11-01')"
            )
        )
        for i in range(5):
            conn.execute(
                text(
                    "INSERT INTO task_events (task_id, event, message, created_at) "
                    "VALUES ('t1', 'STEP_COMPLETED', :m, '2026-11-01')"
                ),
                {"m": f"event {i}"},
            )
        conn.execute(
            text(
                "INSERT INTO tool_calls (task_id, step_id, tool, input_json, status, attempt,"
                " duration_ms, created_at) VALUES ('t1', 'step_1', 'calculator', '{}',"
                " 'SUCCESS', 1, 5, '2026-11-01')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO memories (task_id, kind, content, created_at) "
                "VALUES ('t1', 'result', 'old result', '2026-11-01')"
            )
        )
    return db


def test_legacy_database_is_upgraded_without_losing_data(tmp_path: Path) -> None:
    db = _legacy_milestone1_database(tmp_path / "legacy.db")
    db.migrate()

    assert _revision(db) == _head()
    tables = set(inspect(db.engine).get_table_names())
    assert {"llm_usage", "conversations", "conversation_messages"} <= tables
    with db.engine.connect() as conn:
        # Rows in child tables survive the rebuild of their parent 'tasks' table.
        assert conn.execute(text("SELECT count(*) FROM task_events")).scalar() == 5
        assert conn.execute(text("SELECT count(*) FROM tool_calls")).scalar() == 1
        task = conn.execute(text("SELECT goal, conversation_id FROM tasks")).one()
        assert task == ("old goal", None)
        memory = conn.execute(text("SELECT content, category FROM memories")).one()
        assert memory == ("old result", "general")  # new column filled with its default
        # Foreign keys are switched back on and consistent.
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    db.dispose()


def test_foreign_keys_are_enabled_after_migration(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'fk.db'}")
    db.migrate()
    with db.engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    db.dispose()


def test_downgrade_and_upgrade_round_trip(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'round.db'}"
    db = Database(url)
    db.migrate()
    with db.engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        cfg = _config(url)
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0001")
        conn.commit()
        assert "conversations" not in inspect(conn).get_table_names()
        command.upgrade(cfg, "head")
        conn.commit()
    assert _revision(db) == _head()
    db.dispose()
