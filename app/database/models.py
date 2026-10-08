"""ORM models (SQLite tables)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.database import Base, UTCDateTime, utcnow


def new_id() -> str:
    return uuid.uuid4().hex


class TaskRow(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True)
    plan_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_step_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    iterations: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    monitor_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("monitors.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class TaskEventRow(Base):
    """Execution history: one row per significant agent event."""

    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    event: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    step_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    data_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ToolCallRow(Base):
    __tablename__ = "tool_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    step_id: Mapped[str] = mapped_column(String(32))
    tool: Mapped[str] = mapped_column(String(64))
    input_json: Mapped[str] = mapped_column(Text)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16))  # SUCCESS / ERROR
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ApprovalRow(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    step_id: Mapped[str] = mapped_column(String(32))
    tool: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    explanation: Mapped[str] = mapped_column(Text)
    input_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), index=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class MemoryRow(Base):
    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), index=True)
    content: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class MonitorRow(Base):
    __tablename__ = "monitors"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    goal: Mapped[str] = mapped_column(Text)
    interval_minutes: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_change_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class NotificationRow(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(String(32))
    task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    monitor_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
