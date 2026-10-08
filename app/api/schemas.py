"""Request/response models for the HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.agent.state import Plan, TaskStatus
from app.approvals.manager import Approval
from app.memory.models import MemoryRecord
from app.services.task_service import TaskEvent, TaskView, ToolCall


def _strip_non_empty(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("must not be empty")
    return value


class TaskCreate(BaseModel):
    goal: str = Field(
        min_length=3,
        max_length=5000,
        examples=["Research the latest developments in AI agents and create a summary."],
    )

    _clean = field_validator("goal")(_strip_non_empty)


class TaskCreated(BaseModel):
    task_id: str
    goal: str
    status: TaskStatus
    created_at: datetime


class TaskSummary(BaseModel):
    id: str
    goal: str
    status: TaskStatus
    current_step: str | None
    progress_done: int
    progress_total: int
    monitor_id: str | None
    created_at: datetime
    started_at: datetime | None
    updated_at: datetime
    completed_at: datetime | None

    @classmethod
    def from_view(cls, view: TaskView) -> TaskSummary:
        done, total = view.progress
        return cls(
            id=view.id,
            goal=view.goal,
            status=view.status,
            current_step=view.current_step,
            progress_done=done,
            progress_total=total,
            monitor_id=view.monitor_id,
            created_at=view.created_at,
            started_at=view.started_at,
            updated_at=view.updated_at,
            completed_at=view.completed_at,
        )


class TaskDetail(TaskSummary):
    plan: Plan | None
    iterations: int
    result: str | None
    error: str | None
    events: list[TaskEvent]
    tool_calls: list[ToolCall]
    approvals: list[Approval]
    memory: list[MemoryRecord]


class ApprovalDecision(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class MonitorCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=3, max_length=5000)
    interval_minutes: int = Field(ge=1, le=60 * 24 * 30)
    run_now: bool = True

    _clean_name = field_validator("name")(_strip_non_empty)
    _clean_goal = field_validator("goal")(_strip_non_empty)


class Overview(BaseModel):
    active_tasks: int
    completed_tasks: int
    waiting_approvals: int
    failed_tasks: int
    paused_tasks: int
    monitoring_jobs: int
    unread_notifications: int
    status_counts: dict[str, int]


class Health(BaseModel):
    status: str
    llm_configured: bool
    search_provider: str
    tools: list[dict[str, Any]]
