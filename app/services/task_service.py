"""Task persistence: tasks, plans, status transitions, events and tool calls."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select

from app.agent.state import (
    TERMINAL_STATUSES,
    Clarification,
    EventType,
    Plan,
    TaskStatus,
    can_transition,
)
from app.database.database import Database, escape_like, utcnow
from app.database.models import TaskEventRow, TaskRow, ToolCallRow

logger = logging.getLogger(__name__)

MAX_STORED_OUTPUT = 20_000


class TaskNotFoundError(LookupError):
    pass


class InvalidTransitionError(ValueError):
    pass


class TaskView(BaseModel):
    """Read model for a task."""

    id: str
    goal: str
    status: TaskStatus
    plan: Plan | None
    clarifications: list[Clarification] = []
    current_step_id: str | None
    iterations: int
    result: str | None
    error: str | None
    monitor_id: str | None
    conversation_id: str | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    @property
    def current_step(self) -> str | None:
        if self.plan and self.current_step_id:
            step = self.plan.get_step(self.current_step_id)
            return step.description if step else None
        return None

    @property
    def progress(self) -> tuple[int, int]:
        return self.plan.progress() if self.plan else (0, 0)


class TaskEvent(BaseModel):
    id: int
    event: str
    message: str
    step_id: str | None
    data: dict[str, Any] | None
    created_at: datetime


class ActivityEvent(BaseModel):
    """An event from any task, for the activity feed."""

    id: int
    task_id: str
    task_goal: str
    event: str
    message: str
    step_id: str | None
    created_at: datetime


class ToolCall(BaseModel):
    id: int
    step_id: str
    tool: str
    input: dict[str, Any]
    output: str | None
    status: str
    error: str | None
    attempt: int
    duration_ms: int
    created_at: datetime


def _clarifications(row: TaskRow) -> list[Clarification]:
    if not row.clarifications_json:
        return []
    return [Clarification.model_validate(c) for c in json.loads(row.clarifications_json)]


def _dump(items: list[Clarification]) -> str:
    return json.dumps([c.model_dump(mode="json") for c in items])


def _view(row: TaskRow) -> TaskView:
    return TaskView(
        id=row.id,
        goal=row.goal,
        status=TaskStatus(row.status),
        plan=Plan.model_validate_json(row.plan_json) if row.plan_json else None,
        clarifications=_clarifications(row),
        current_step_id=row.current_step_id,
        iterations=row.iterations,
        result=row.result,
        error=row.error,
        monitor_id=row.monitor_id,
        conversation_id=row.conversation_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


class TaskService:
    """All task-related reads and writes go through this class."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # --- tasks ----------------------------------------------------------------
    def create_task(
        self, goal: str, monitor_id: str | None = None, conversation_id: str | None = None
    ) -> TaskView:
        row = TaskRow(
            goal=goal,
            status=TaskStatus.PENDING.value,
            monitor_id=monitor_id,
            conversation_id=conversation_id,
        )
        with self._db.session() as session:
            session.add(row)
            session.flush()
            session.add(
                TaskEventRow(
                    task_id=row.id, event=EventType.TASK_CREATED.value, message="Task created"
                )
            )
            view = _view(row)
        logger.info("task created", extra={"task_id": view.id, "event": "TASK_CREATED"})
        return view

    def get_task(self, task_id: str) -> TaskView:
        with self._db.session() as session:
            row = session.get(TaskRow, task_id)
            if row is None:
                raise TaskNotFoundError(task_id)
            return _view(row)

    def list_tasks(
        self,
        status: TaskStatus | None = None,
        limit: int = 100,
        monitor_id: str | None = None,
        conversation_id: str | None = None,
        statuses: set[TaskStatus] | None = None,
        query: str | None = None,
        offset: int = 0,
    ) -> list[TaskView]:
        stmt = select(TaskRow).order_by(TaskRow.created_at.desc()).offset(offset).limit(limit)
        if status is not None:
            stmt = stmt.where(TaskRow.status == status.value)
        if statuses:
            stmt = stmt.where(TaskRow.status.in_([s.value for s in statuses]))
        if query and query.strip():
            pattern = escape_like(query.strip())
            stmt = stmt.where(TaskRow.goal.ilike(f"%{pattern}%", escape="\\"))
        if monitor_id is not None:
            stmt = stmt.where(TaskRow.monitor_id == monitor_id)
        if conversation_id is not None:
            stmt = stmt.where(TaskRow.conversation_id == conversation_id)
        with self._db.session() as session:
            return [_view(r) for r in session.scalars(stmt)]

    def ids_with_status(self, statuses: set[TaskStatus]) -> list[str]:
        stmt = select(TaskRow.id).where(TaskRow.status.in_([s.value for s in statuses]))
        with self._db.session() as session:
            return list(session.scalars(stmt))

    def status_counts(self) -> dict[str, int]:
        stmt = select(TaskRow.status, func.count(TaskRow.id)).group_by(TaskRow.status)
        with self._db.session() as session:
            counts = {status: count for status, count in session.execute(stmt)}
        return {s.value: int(counts.get(s.value, 0)) for s in TaskStatus}

    # --- status ---------------------------------------------------------------
    def transition(self, task_id: str, new: TaskStatus, **fields: Any) -> TaskView:
        """Change status, raising InvalidTransitionError if not allowed."""
        with self._db.session() as session:
            row = session.get(TaskRow, task_id)
            if row is None:
                raise TaskNotFoundError(task_id)
            current = TaskStatus(row.status)
            if current == new:
                raise InvalidTransitionError(f"Task is already {current}")
            if not can_transition(current, new):
                raise InvalidTransitionError(f"Cannot move task from {current} to {new}")
            self._apply(row, new, fields)
            return _view(row)

    def try_transition(
        self, task_id: str, allowed_from: set[TaskStatus], new: TaskStatus, **fields: Any
    ) -> bool:
        """Change status only if the current status is in ``allowed_from``.

        Used by the agent loop so it never overwrites a status set concurrently
        by a user (e.g. PAUSED or CANCELLED).
        """
        with self._db.session() as session:
            row = session.get(TaskRow, task_id)
            if row is None:
                raise TaskNotFoundError(task_id)
            current = TaskStatus(row.status)
            if current not in allowed_from:
                return False
            if current != new and not can_transition(current, new):
                return False
            self._apply(row, new, fields)
            return True

    @staticmethod
    def _apply(row: TaskRow, new: TaskStatus, fields: dict[str, Any]) -> None:
        now = utcnow()
        row.status = new.value
        row.updated_at = now
        if new in (TaskStatus.PLANNING, TaskStatus.RUNNING) and row.started_at is None:
            row.started_at = now
        if new in TERMINAL_STATUSES:
            row.completed_at = now
        for key, value in fields.items():
            setattr(row, key, value)

    # --- clarifications --------------------------------------------------------
    def request_clarification(self, task_id: str, question: str) -> bool:
        """Record the planner's question and move PLANNING -> WAITING_INPUT.

        Returns False (and records nothing) if the task is no longer PLANNING,
        e.g. because the user paused or cancelled it meanwhile.
        """
        with self._db.session() as session:
            row = session.get(TaskRow, task_id)
            if row is None:
                raise TaskNotFoundError(task_id)
            if not can_transition(TaskStatus(row.status), TaskStatus.WAITING_INPUT):
                return False
            items = _clarifications(row)
            items.append(Clarification(question=question, asked_at=utcnow()))
            row.clarifications_json = _dump(items)
            self._apply(row, TaskStatus.WAITING_INPUT, {})
            session.add(
                TaskEventRow(
                    task_id=task_id,
                    event=EventType.CLARIFICATION_REQUESTED.value,
                    message=question[:2000],
                )
            )
            return True

    def answer_clarification(self, task_id: str, answer: str) -> TaskView:
        """Store the answer to the open question and move WAITING_INPUT -> PENDING."""
        with self._db.session() as session:
            row = session.get(TaskRow, task_id)
            if row is None:
                raise TaskNotFoundError(task_id)
            current = TaskStatus(row.status)
            if current is not TaskStatus.WAITING_INPUT:
                raise InvalidTransitionError(f"Task is {current}, not WAITING_INPUT")
            items = _clarifications(row)
            if not items or items[-1].answer is not None:
                raise InvalidTransitionError("Task has no open question")
            items[-1].answer = answer
            items[-1].answered_at = utcnow()
            row.clarifications_json = _dump(items)
            self._apply(row, TaskStatus.PENDING, {})
            session.add(
                TaskEventRow(
                    task_id=task_id,
                    event=EventType.CLARIFICATION_ANSWERED.value,
                    message=answer[:2000],
                )
            )
            return _view(row)

    # --- plan / iterations ----------------------------------------------------
    def save_plan(self, task_id: str, plan: Plan, current_step_id: str | None = None) -> None:
        with self._db.session() as session:
            row = session.get(TaskRow, task_id)
            if row is None:
                raise TaskNotFoundError(task_id)
            row.plan_json = plan.model_dump_json()
            row.current_step_id = current_step_id
            row.updated_at = utcnow()

    def increment_iterations(self, task_id: str) -> int:
        with self._db.session() as session:
            row = session.get(TaskRow, task_id)
            if row is None:
                raise TaskNotFoundError(task_id)
            row.iterations += 1
            row.updated_at = utcnow()
            return row.iterations

    # --- events ---------------------------------------------------------------
    def add_event(
        self,
        task_id: str,
        event: EventType,
        message: str,
        step_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        with self._db.session() as session:
            session.add(
                TaskEventRow(
                    task_id=task_id,
                    event=event.value,
                    message=message[:4000],
                    step_id=step_id,
                    data_json=json.dumps(data, default=str) if data else None,
                )
            )
        logger.info(message[:500], extra={"task_id": task_id, "event": event.value})

    def list_events(self, task_id: str, limit: int = 500) -> list[TaskEvent]:
        stmt = (
            select(TaskEventRow)
            .where(TaskEventRow.task_id == task_id)
            .order_by(TaskEventRow.id.asc())
            .limit(limit)
        )
        with self._db.session() as session:
            return [
                TaskEvent(
                    id=r.id,
                    event=r.event,
                    message=r.message,
                    step_id=r.step_id,
                    data=json.loads(r.data_json) if r.data_json else None,
                    created_at=r.created_at,
                )
                for r in session.scalars(stmt)
            ]

    def recent_events(
        self, limit: int = 50, before_id: int | None = None, events: set[str] | None = None
    ) -> list[ActivityEvent]:
        """Newest events across all tasks (``before_id`` pages further back)."""
        stmt = (
            select(TaskEventRow, TaskRow.goal)
            .join(TaskRow, TaskRow.id == TaskEventRow.task_id)
            .order_by(TaskEventRow.id.desc())
            .limit(limit)
        )
        if before_id is not None:
            stmt = stmt.where(TaskEventRow.id < before_id)
        if events:
            stmt = stmt.where(TaskEventRow.event.in_(events))
        with self._db.session() as session:
            return [
                ActivityEvent(
                    id=r.id,
                    task_id=r.task_id,
                    task_goal=goal,
                    event=r.event,
                    message=r.message,
                    step_id=r.step_id,
                    created_at=r.created_at,
                )
                for r, goal in session.execute(stmt)
            ]

    # --- tool calls -----------------------------------------------------------
    def record_tool_call(
        self,
        *,
        task_id: str,
        step_id: str,
        tool: str,
        tool_input: dict[str, Any],
        output: str | None,
        status: str,
        error: str | None,
        attempt: int,
        duration_ms: int,
    ) -> None:
        with self._db.session() as session:
            session.add(
                ToolCallRow(
                    task_id=task_id,
                    step_id=step_id,
                    tool=tool,
                    input_json=json.dumps(tool_input, default=str),
                    output=output[:MAX_STORED_OUTPUT] if output else None,
                    status=status,
                    error=error,
                    attempt=attempt,
                    duration_ms=duration_ms,
                )
            )
        logger.info(
            "tool call",
            extra={"task_id": task_id, "tool": tool, "status": status, "error": error},
        )

    def list_tool_calls(self, task_id: str) -> list[ToolCall]:
        stmt = select(ToolCallRow).where(ToolCallRow.task_id == task_id).order_by(ToolCallRow.id)
        with self._db.session() as session:
            return [
                ToolCall(
                    id=r.id,
                    step_id=r.step_id,
                    tool=r.tool,
                    input=json.loads(r.input_json),
                    output=r.output,
                    status=r.status,
                    error=r.error,
                    attempt=r.attempt,
                    duration_ms=r.duration_ms,
                    created_at=r.created_at,
                )
                for r in session.scalars(stmt)
            ]
