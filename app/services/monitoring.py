"""Experimental monitoring mode.

A *monitor* is a goal that is re-run on an interval. Each cycle:

1. wake up (the scheduler polls for due monitors),
2. run the goal as a normal agent task,
3. when the task finishes, compare its result with the previous result,
4. decide (with the LLM) whether something meaningful changed,
5. create a dashboard notification only if it did,
6. save the result as the new baseline,
7. sleep until the next scheduled run.

The scheduler is a single asyncio loop inside the API process. It is not a
distributed scheduler: run one Dot Lab instance per database.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import select

from app.agent.evaluator import Evaluator
from app.agent.llm import LLMError
from app.agent.state import TERMINAL_STATUSES, EventType, TaskStatus
from app.database.database import Database, utcnow
from app.database.models import MonitorRow
from app.services.runner import TaskRunner
from app.services.task_service import TaskService
from app.tools.notifications import NotificationStore

logger = logging.getLogger(__name__)


class MonitorNotFoundError(LookupError):
    pass


class Monitor(BaseModel):
    id: str
    name: str
    goal: str
    interval_minutes: int
    enabled: bool
    last_run_at: datetime | None
    next_run_at: datetime | None
    last_task_id: str | None
    last_result: str | None
    last_change_summary: str | None
    created_at: datetime


def _model(row: MonitorRow) -> Monitor:
    return Monitor.model_validate(row, from_attributes=True)


class MonitorService:
    def __init__(
        self,
        db: Database,
        tasks: TaskService,
        runner: TaskRunner,
        evaluator: Evaluator,
        notifications: NotificationStore,
    ) -> None:
        self._db = db
        self.tasks = tasks
        self.runner = runner
        self.evaluator = evaluator
        self.notifications = notifications
        runner.add_listener(self.on_task_finished)

    # --- CRUD -------------------------------------------------------------------
    def create(self, name: str, goal: str, interval_minutes: int, run_now: bool = True) -> Monitor:
        now = utcnow()
        row = MonitorRow(
            name=name,
            goal=goal,
            interval_minutes=interval_minutes,
            enabled=True,
            next_run_at=now if run_now else now + timedelta(minutes=interval_minutes),
        )
        with self._db.session() as session:
            session.add(row)
            session.flush()
            return _model(row)

    def get(self, monitor_id: str) -> Monitor:
        with self._db.session() as session:
            row = session.get(MonitorRow, monitor_id)
            if row is None:
                raise MonitorNotFoundError(monitor_id)
            return _model(row)

    def list(self) -> list[Monitor]:
        with self._db.session() as session:
            rows = session.scalars(select(MonitorRow).order_by(MonitorRow.created_at.desc()))
            return [_model(r) for r in rows]

    def set_enabled(self, monitor_id: str, enabled: bool) -> Monitor:
        with self._db.session() as session:
            row = session.get(MonitorRow, monitor_id)
            if row is None:
                raise MonitorNotFoundError(monitor_id)
            row.enabled = enabled
            if enabled and row.next_run_at is None:
                row.next_run_at = utcnow()
            return _model(row)

    def delete(self, monitor_id: str) -> None:
        with self._db.session() as session:
            row = session.get(MonitorRow, monitor_id)
            if row is None:
                raise MonitorNotFoundError(monitor_id)
            session.delete(row)

    # --- execution ----------------------------------------------------------------
    def _has_active_run(self, monitor: Monitor) -> bool:
        if not monitor.last_task_id:
            return False
        try:
            return self.tasks.get_task(monitor.last_task_id).status not in TERMINAL_STATUSES
        except LookupError:
            return False

    def trigger(self, monitor_id: str) -> str | None:
        """Start a monitor cycle now. Returns the task id, or None if one is still running."""
        monitor = self.get(monitor_id)
        if self._has_active_run(monitor):
            return None
        task = self.tasks.create_task(monitor.goal, monitor_id=monitor.id)
        now = utcnow()
        with self._db.session() as session:
            row = session.get(MonitorRow, monitor_id)
            if row is not None:
                row.last_task_id = task.id
                row.last_run_at = now
                row.next_run_at = now + timedelta(minutes=row.interval_minutes)
        self.runner.submit(task.id)
        logger.info("monitor triggered", extra={"task_id": task.id, "event": "MONITOR_RUN"})
        return task.id

    def due_monitors(self, now: datetime | None = None) -> list[Monitor]:
        now = now or utcnow()
        stmt = select(MonitorRow).where(MonitorRow.enabled.is_(True), MonitorRow.next_run_at <= now)
        with self._db.session() as session:
            return [_model(r) for r in session.scalars(stmt)]

    async def run_due(self) -> list[str]:
        """Trigger every due monitor; returns the created task ids."""
        started = []
        for monitor in self.due_monitors():
            task_id = self.trigger(monitor.id)
            if task_id:
                started.append(task_id)
            else:
                # Previous run still in progress: push the schedule forward.
                self._reschedule(monitor.id)
        return started

    def _reschedule(self, monitor_id: str) -> None:
        with self._db.session() as session:
            row = session.get(MonitorRow, monitor_id)
            if row is not None:
                row.next_run_at = utcnow() + timedelta(minutes=row.interval_minutes)

    async def on_task_finished(self, task_id: str, status: TaskStatus) -> None:
        """Runner listener: compare results when a monitor's task completes."""
        task = self.tasks.get_task(task_id)
        if not task.monitor_id or status is not TaskStatus.COMPLETED or not task.result:
            return
        try:
            monitor = self.get(task.monitor_id)
        except MonitorNotFoundError:
            return

        if not monitor.last_result:
            summary, changed = "Baseline captured (first run).", False
        else:
            try:
                assessment = await self.evaluator.compare_results(
                    monitor.goal, monitor.last_result, task.result
                )
                summary, changed = assessment.summary, assessment.meaningful_change
            except LLMError as exc:
                logger.warning(
                    "monitor comparison failed", extra={"task_id": task_id, "error": str(exc)}
                )
                summary, changed = f"Comparison failed: {exc}", False

        with self._db.session() as session:
            row = session.get(MonitorRow, monitor.id)
            if row is not None:
                row.last_result = task.result
                row.last_change_summary = summary
        self.tasks.add_event(
            task_id,
            EventType.MONITOR_RESULT,
            f"{'Meaningful change' if changed else 'No notification'}: {summary}",
        )
        if changed:
            self.notifications.create(
                title=f"Monitor update: {monitor.name}",
                message=summary,
                channel="dashboard",
                task_id=task_id,
                monitor_id=monitor.id,
            )


class MonitorScheduler:
    """Polls for due monitors every ``poll_seconds``."""

    def __init__(self, service: MonitorService, poll_seconds: int) -> None:
        self.service = service
        self.poll_seconds = poll_seconds
        self._job: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._job is None or self._job.done():
            self._job = asyncio.create_task(self._loop(), name="dotlab-monitor-scheduler")

    async def _loop(self) -> None:
        while True:
            try:
                await self.service.run_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("monitor scheduler iteration failed")
            await asyncio.sleep(self.poll_seconds)

    async def stop(self) -> None:
        if self._job is not None:
            self._job.cancel()
            await asyncio.gather(self._job, return_exceptions=True)
            self._job = None
