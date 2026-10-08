"""Persistent memory.

``MemoryStore`` is the interface the agent depends on. ``SQLiteMemoryStore``
implements it with simple keyword search. A vector-database implementation can
be added later by implementing the same four methods.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod

from sqlalchemy import or_, select

from app.database.database import Database
from app.database.models import MemoryRow
from app.memory.models import MemoryCreate, MemoryKind, MemoryRecord

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
        limit: int = 50,
    ) -> list[MemoryRecord]: ...

    @abstractmethod
    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        kinds: list[MemoryKind] | None = None,
        exclude_task_id: str | None = None,
    ) -> list[MemoryRecord]: ...

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
        limit: int = 50,
    ) -> list[MemoryRecord]:
        stmt = select(MemoryRow)
        if memory_id is not None:
            stmt = stmt.where(MemoryRow.id == memory_id)
        if task_id is not None:
            stmt = stmt.where(MemoryRow.task_id == task_id)
        if kind is not None:
            stmt = stmt.where(MemoryRow.kind == kind.value)
        stmt = stmt.order_by(MemoryRow.id.desc()).limit(limit)
        with self._db.session() as session:
            return [_to_record(row) for row in session.scalars(stmt)]

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        kinds: list[MemoryKind] | None = None,
        exclude_task_id: str | None = None,
    ) -> list[MemoryRecord]:
        """Keyword search ranked by the number of matching query terms."""
        terms = _terms(query)
        if not terms:
            return []
        stmt = select(MemoryRow).where(
            or_(*[MemoryRow.content.ilike(f"%{term}%") for term in terms])
        )
        if kinds:
            stmt = stmt.where(MemoryRow.kind.in_([k.value for k in kinds]))
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
            record = _to_record(row)
            record.score = hits / len(terms)
            scored.append(record)
        scored.sort(key=lambda r: (r.score or 0, r.id), reverse=True)
        return scored[:limit]

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
        content=row.content,
        task_id=row.task_id,
        metadata=json.loads(row.metadata_json) if row.metadata_json else {},
        created_at=row.created_at,
    )
