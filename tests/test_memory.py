"""Memory store tests."""

from __future__ import annotations

import pytest

from app.database.database import Database
from app.memory.memory import SQLiteMemoryStore
from app.memory.models import MemoryCreate, MemoryKind


@pytest.fixture
def store(tmp_path):  # type: ignore[no-untyped-def]
    db = Database(f"sqlite:///{tmp_path / 'mem.db'}")
    db.create_all()
    yield SQLiteMemoryStore(db)
    db.dispose()


def test_memory_save(store: SQLiteMemoryStore) -> None:
    record = store.save(
        MemoryCreate(kind=MemoryKind.OBSERVATION, content="Agents use tools", metadata={"a": 1})
    )
    assert record.id > 0
    assert record.kind is MemoryKind.OBSERVATION
    assert record.metadata == {"a": 1}
    assert record.created_at.tzinfo is not None


def test_memory_retrieval(store: SQLiteMemoryStore) -> None:
    first = store.save(MemoryCreate(kind=MemoryKind.GOAL, content="goal one"))
    store.save(MemoryCreate(kind=MemoryKind.RESULT, content="result one"))

    assert store.retrieve(memory_id=first.id)[0].content == "goal one"
    assert [r.content for r in store.retrieve(kind=MemoryKind.RESULT)] == ["result one"]
    assert [r.content for r in store.retrieve()] == ["result one", "goal one"]  # newest first


def test_memory_search_ranks_by_term_overlap(store: SQLiteMemoryStore) -> None:
    store.save(MemoryCreate(kind=MemoryKind.OBSERVATION, content="Python is popular"))
    store.save(
        MemoryCreate(kind=MemoryKind.OBSERVATION, content="Autonomous agents plan with tools")
    )
    store.save(MemoryCreate(kind=MemoryKind.RESULT, content="Agents need memory"))

    results = store.search("autonomous agents tools")
    assert results[0].content == "Autonomous agents plan with tools"
    assert {r.content for r in results} == {
        "Autonomous agents plan with tools",
        "Agents need memory",
    }
    only_results = store.search("agents", kinds=[MemoryKind.RESULT])
    assert [r.content for r in only_results] == ["Agents need memory"]
    assert store.search("") == []


def test_memory_search_can_exclude_task(store: SQLiteMemoryStore, tmp_path) -> None:  # type: ignore[no-untyped-def]
    store.save(MemoryCreate(kind=MemoryKind.NOTE, content="global agents note"))
    assert len(store.search("agents", exclude_task_id="abc")) == 1


def test_memory_delete(store: SQLiteMemoryStore) -> None:
    record = store.save(MemoryCreate(kind=MemoryKind.NOTE, content="temporary"))
    assert store.delete(record.id) is True
    assert store.retrieve(memory_id=record.id) == []
    assert store.delete(record.id) is False
