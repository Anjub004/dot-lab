"""Persistent long-term memory.

``MemoryStore`` is the interface the agent and API depend on.
``SQLiteMemoryStore`` implements it with keyword search. A vector-database
implementation can be added later by implementing the same methods.

Memories have a ``kind`` (what produced it: a task result, a reviewer
observation, a user note, ...) and a ``category`` (what it is about, e.g.
``sales`` or ``suppliers``) that users can set and change.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod

from sqlalchemy import func, or_, select

from app.database.database import Database, utcnow
from app.database.models import MemoryRow
from app.memory.models import (
    CategoryCount,
    MemoryCreate,
    MemoryKind,
    MemoryRecord,
    normalize_category,
)

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]{2,}")
_STOPWORDS = frozenset(
    [
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "into",
        "about",
        "what",
        "when",
        "where",
        "which",
        "have",
        "has",
        "are",
        "was",
        "were",
        "will",
        "would",
        "should",
        "could",
        "your",
        "you",
        "our",
        "their",
        "them",
        "then",
        "than",
        "also",
        "latest",
        "create",
        "make",
        "prepare",
        "summary",
    ]
)
MAX_CONTENT_CHARS = 20_000


def _escape_like(term: str) -> str:
    """Escape LIKE wildcards so ``_`` and ``%`` in a search term match literally."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _terms(query: str) -> list[str]:
    words = {w.lower() for w in _WORD.findall(query)}
    return sorted(w for w in words if w not in _STOPWORDS)[:12]


class MemoryStore(ABC):
    """Abstract memory interface."""

    @abstractmethod
    def save(self, memory: MemoryCreate) -> MemoryRecord: ...

    @abstractmethod
    def retrieve(
        self,
        *,
        memory_id: int | None = None,
        task_id: str | None = None,
        kind: MemoryKind | None = None,
        category: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[MemoryRecord]: ...

    @abstractmethod
    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        kinds: list[MemoryKind] | None = None,
        category: str | None = None,
        exclude_task_id: str | None = None,
    ) -> list[MemoryRecord]: ...

    @abstractmethod
    def update(
        self, memory_id: int, *, content: str | None = None, category: str | None = None
    ) -> MemoryRecord | None: ...

    @abstractmethod
    def categories(self) -> list[CategoryCount]: ...

    @abstractmethod
    def delete(self, memory_id: int) -> bool: ...


class SQLiteMemoryStore(MemoryStore):
    """Memory stored in the ``memories`` SQLite table."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def save(self, memory: MemoryCreate) -> MemoryRecord:
        row = MemoryRow(
            task_id=memory.task_id,
            kind=memory.kind.value,
            category=memory.category,
            content=memory.content[:MAX_CONTENT_CHARS],
            metadata_json=json.dumps(memory.metadata, default=str),
        )
        with self._db.session() as session:
            session.add(row)
            session.flush()
            return _to_record(row)

    def retrieve(
        self,
        *,
        memory_id: int | None = None,
        task_id: str | None = None,
        kind: MemoryKind | None = None,
        category: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[MemoryRecord]:
        stmt = select(MemoryRow)
        if memory_id is not None:
            stmt = stmt.where(MemoryRow.id == memory_id)
        if task_id is not None:
            stmt = stmt.where(MemoryRow.task_id == task_id)
        if kind is not None:
            stmt = stmt.where(MemoryRow.kind == kind.value)
        if category is not None:
            stmt = stmt.where(MemoryRow.category == normalize_category(category))
        stmt = stmt.order_by(MemoryRow.id.desc()).offset(offset).limit(limit)
        with self._db.session() as session:
            return [_to_record(row) for row in session.scalars(stmt)]

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        kinds: list[MemoryKind] | None = None,
        category: str | None = None,
        exclude_task_id: str | None = None,
    ) -> list[MemoryRecord]:
        """Keyword search ranked by the number of matching query terms."""
        terms = _terms(query)
        if not terms:
            return []
        stmt = select(MemoryRow).where(
            or_(
                *[MemoryRow.content.ilike(f"%{_escape_like(term)}%", escape="\\") for term in terms]
            )
        )
        if kinds:
            stmt = stmt.where(MemoryRow.kind.in_([k.value for k in kinds]))
        if category is not None:
            stmt = stmt.where(MemoryRow.category == normalize_category(category))
        if exclude_task_id:
            stmt = stmt.where(
                or_(MemoryRow.task_id.is_(None), MemoryRow.task_id != exclude_task_id)
            )
        stmt = stmt.order_by(MemoryRow.id.desc()).limit(500)

        with self._db.session() as session:
            rows = list(session.scalars(stmt))

        scored: list[MemoryRecord] = []
        for row in rows:
            text = row.content.lower()
            hits = sum(1 for term in terms if term in text)
            if hits == 0:  # defensive: never return records that match no term
                continue
            record = _to_record(row)
            record.score = hits / len(terms)
            scored.append(record)
        scored.sort(key=lambda r: (r.score or 0, r.id), reverse=True)
        return scored[:limit]

    def update(
        self, memory_id: int, *, content: str | None = None, category: str | None = None
    ) -> MemoryRecord | None:
        """Change a memory's content and/or category. Returns None if it does not exist."""
        if content is not None and not content.strip():
            raise ValueError("content must not be empty")
        with self._db.session() as session:
            row = session.get(MemoryRow, memory_id)
            if row is None:
                return None
            if content is not None:
                row.content = content[:MAX_CONTENT_CHARS]
            if category is not None:
                row.category = normalize_category(category)
            row.updated_at = utcnow()
            session.flush()
            return _to_record(row)

    def categories(self) -> list[CategoryCount]:
        stmt = (
            select(MemoryRow.category, func.count(MemoryRow.id))
            .group_by(MemoryRow.category)
            .order_by(func.count(MemoryRow.id).desc(), MemoryRow.category)
        )
        with self._db.session() as session:
            return [CategoryCount(category=c, count=n) for c, n in session.execute(stmt)]

    def delete(self, memory_id: int) -> bool:
        with self._db.session() as session:
            row = session.get(MemoryRow, memory_id)
            if row is None:
                return False
            session.delete(row)
            return True


def _to_record(row: MemoryRow) -> MemoryRecord:
    return MemoryRecord(
        id=row.id,
        kind=MemoryKind(row.kind),
        category=row.category,
        content=row.content,
        task_id=row.task_id,
        metadata=json.loads(row.metadata_json) if row.metadata_json else {},
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
