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


def test_memory_search_treats_wildcards_literally(store: SQLiteMemoryStore) -> None:
    """'_' is a LIKE wildcard; a search for 'sales_report' must not match 'salesXreport'."""
    store.save(MemoryCreate(kind=MemoryKind.NOTE, content="salesXreport for March"))
    exact = store.save(MemoryCreate(kind=MemoryKind.NOTE, content="saved sales_report.csv"))

    results = store.search("sales_report")
    assert [r.id for r in results] == [exact.id]
    assert all((r.score or 0) > 0 for r in results)


# ------------------------------------------------------- categories & updates --


def test_category_is_normalized_and_validated(store: SQLiteMemoryStore) -> None:
    record = store.save(
        MemoryCreate(kind=MemoryKind.NOTE, content="x", category="  Sales Reports ")
    )
    assert record.category == "sales_reports"
    assert store.save(MemoryCreate(kind=MemoryKind.NOTE, content="y")).category == "general"
    for bad in ["", "-starts-with-dash", "a" * 51, "slash/category", "dot.category"]:
        with pytest.raises(ValueError):
            MemoryCreate(kind=MemoryKind.NOTE, content="z", category=bad)


def test_list_by_category_with_paging(store: SQLiteMemoryStore) -> None:
    for i in range(5):
        store.save(MemoryCreate(kind=MemoryKind.NOTE, content=f"sales {i}", category="sales"))
    store.save(MemoryCreate(kind=MemoryKind.NOTE, content="supplier fact", category="suppliers"))

    first = store.retrieve(category="sales", limit=2)
    second = store.retrieve(category="Sales", limit=2, offset=2)  # normalized too
    assert [r.content for r in first] == ["sales 4", "sales 3"]
    assert [r.content for r in second] == ["sales 2", "sales 1"]
    assert [(c.category, c.count) for c in store.categories()] == [("sales", 5), ("suppliers", 1)]


def test_search_within_category(store: SQLiteMemoryStore) -> None:
    store.save(MemoryCreate(kind=MemoryKind.NOTE, content="Northgate target", category="sales"))
    store.save(MemoryCreate(kind=MemoryKind.NOTE, content="Northgate supplier", category="ops"))
    assert [r.category for r in store.search("northgate", category="ops")] == ["ops"]
    assert len(store.search("northgate")) == 2


def test_memory_update(store: SQLiteMemoryStore) -> None:
    record = store.save(MemoryCreate(kind=MemoryKind.NOTE, content="old text"))
    assert record.updated_at is None

    updated = store.update(record.id, content="new text", category="Finance")
    assert updated is not None
    assert (updated.content, updated.category) == ("new text", "finance")
    assert updated.updated_at is not None and updated.created_at == record.created_at
    assert store.retrieve(memory_id=record.id)[0].content == "new text"

    only_category = store.update(record.id, category="ops")
    assert only_category is not None and only_category.content == "new text"
    assert store.update(999_999, content="x") is None
    with pytest.raises(ValueError):
        store.update(record.id, content="   ")


def test_memory_persists_across_restarts(tmp_path) -> None:  # type: ignore[no-untyped-def]
    url = f"sqlite:///{tmp_path / 'restart.db'}"
    first = Database(url)
    first.migrate()
    saved = SQLiteMemoryStore(first).save(
        MemoryCreate(
            kind=MemoryKind.NOTE, content="Fiscal year starts in April", category="finance"
        )
    )
    first.dispose()  # simulate the process stopping

    second = Database(url)
    second.migrate()  # start-up runs migrations again: must be a no-op
    again = SQLiteMemoryStore(second).retrieve(memory_id=saved.id)
    assert again and again[0].content == "Fiscal year starts in April"
    assert again[0].category == "finance"
    second.dispose()
