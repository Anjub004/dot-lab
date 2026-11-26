"""Conversations: short-term context for follow-up requests.

A conversation is a chat thread. Each user message starts a task; when the
task finishes, its result (or failure) is added as the assistant's reply. When a
new task is planned, the most recent earlier messages of its conversation are
given to the planner and reviewer, so a follow-up such as "now do the same for
June" can be understood.

This is *short-term* context: it only applies inside one conversation. Facts
worth keeping across conversations belong in long-term memory instead.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import func, select

from app.database.database import Database, utcnow
from app.database.models import ConversationMessageRow, ConversationRow

DEFAULT_TITLE = "New conversation"
CONTEXT_MAX_CHARS = 8000
MESSAGE_MAX_CHARS = 2000  # per message inside the context, so one long reply can't crowd out others


class ConversationNotFoundError(LookupError):
    pass


class Conversation(BaseModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    message_count: int = 0


class ConversationMessage(BaseModel):
    id: int
    conversation_id: str
    role: Literal["user", "assistant"]
    content: str
    task_id: str | None
    created_at: datetime


class ConversationService:
    def __init__(self, db: Database) -> None:
        self._db = db

    def create(self, title: str | None = None) -> Conversation:
        row = ConversationRow(title=(title or "").strip()[:200] or DEFAULT_TITLE)
        with self._db.session() as session:
            session.add(row)
            session.flush()
            return _conversation(row, 0)

    def get(self, conversation_id: str) -> Conversation:
        with self._db.session() as session:
            row = session.get(ConversationRow, conversation_id)
            if row is None:
                raise ConversationNotFoundError(conversation_id)
            count = session.scalar(
                select(func.count(ConversationMessageRow.id)).where(
                    ConversationMessageRow.conversation_id == conversation_id
                )
            )
            return _conversation(row, int(count or 0))

    def list(self, limit: int = 50) -> list[Conversation]:
        counts = (
            select(
                ConversationMessageRow.conversation_id,
                func.count(ConversationMessageRow.id).label("n"),
            )
            .group_by(ConversationMessageRow.conversation_id)
            .subquery()
        )
        stmt = (
            select(ConversationRow, func.coalesce(counts.c.n, 0))
            .outerjoin(counts, counts.c.conversation_id == ConversationRow.id)
            .order_by(ConversationRow.updated_at.desc())
            .limit(limit)
        )
        with self._db.session() as session:
            return [_conversation(row, int(n)) for row, n in session.execute(stmt)]

    def delete(self, conversation_id: str) -> bool:
        """Delete a conversation and its messages. Tasks are kept (their link is cleared)."""
        with self._db.session() as session:
            row = session.get(ConversationRow, conversation_id)
            if row is None:
                return False
            session.delete(row)
            return True

    def add_message(
        self,
        conversation_id: str,
        role: Literal["user", "assistant"],
        content: str,
        task_id: str | None = None,
    ) -> ConversationMessage:
        with self._db.session() as session:
            conversation = session.get(ConversationRow, conversation_id)
            if conversation is None:
                raise ConversationNotFoundError(conversation_id)
            row = ConversationMessageRow(
                conversation_id=conversation_id, role=role, content=content, task_id=task_id
            )
            session.add(row)
            conversation.updated_at = utcnow()
            if role == "user" and conversation.title == DEFAULT_TITLE:
                conversation.title = _title_from(content)
            session.flush()
            return _message(row)

    def messages(self, conversation_id: str, limit: int = 200) -> list[ConversationMessage]:
        """Messages oldest first (the newest ``limit`` messages)."""
        stmt = (
            select(ConversationMessageRow)
            .where(ConversationMessageRow.conversation_id == conversation_id)
            .order_by(ConversationMessageRow.id.desc())
            .limit(limit)
        )
        with self._db.session() as session:
            rows = list(session.scalars(stmt))
        return [_message(r) for r in reversed(rows)]

    def context_for_task(self, conversation_id: str, task_id: str, max_messages: int) -> str:
        """Earlier messages of the conversation, formatted for a prompt.

        Messages that belong to the task itself (its own request and reply) are
        excluded. The newest messages are kept when the text is too long.
        """
        if max_messages <= 0:
            return ""
        stmt = (
            select(ConversationMessageRow)
            .where(
                ConversationMessageRow.conversation_id == conversation_id,
                (ConversationMessageRow.task_id.is_(None))
                | (ConversationMessageRow.task_id != task_id),
            )
            .order_by(ConversationMessageRow.id.desc())
            .limit(max_messages)
        )
        with self._db.session() as session:
            rows = list(session.scalars(stmt))
        lines: list[str] = []
        used = 0
        for row in rows:  # newest first, so the most recent context survives truncation
            content = row.content.strip()
            if len(content) > MESSAGE_MAX_CHARS:
                content = content[:MESSAGE_MAX_CHARS] + " [...]"
            line = f"{row.role.upper()}: {content}"
            if used + len(line) > CONTEXT_MAX_CHARS:
                break
            lines.append(line)
            used += len(line)
        return "\n\n".join(reversed(lines))


def _title_from(content: str) -> str:
    text = " ".join(content.split())
    return text if len(text) <= 60 else text[:57].rstrip() + "..."


def _conversation(row: ConversationRow, count: int) -> Conversation:
    return Conversation(
        id=row.id,
        title=row.title,
        created_at=row.created_at,
        updated_at=row.updated_at,
        message_count=count,
    )


def _message(row: ConversationMessageRow) -> ConversationMessage:
    return ConversationMessage(
        id=row.id,
        conversation_id=row.conversation_id,
        role=row.role,  # type: ignore[arg-type]
        content=row.content,
        task_id=row.task_id,
        created_at=row.created_at,
    )
