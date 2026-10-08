"""Background task runner (asyncio).

V1 runs agent tasks as ``asyncio`` tasks inside the API process, limited by a
semaphore. This is simple and good for local experimentation, but it is NOT
durable: if the process stops, in-flight work stops too. On startup,
:meth:`TaskRunner.recover` re-queues tasks that were interrupted.

For production, replace this class with a durable queue (Celery, RQ, Arq,
Dramatiq, ...). The rest of the system only calls ``submit``, ``cancel`` and
``add_listener``, so the swap is localised.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.agent.agent import Agent
from app.agent.state import EventType, TaskStatus
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)

FinishedListener = Callable[[str, TaskStatus], Awaitable[None]]


class TaskRunner:
    def __init__(self, agent: Agent, tasks: TaskService, max_concurrent: int) -> None:
        self.agent = agent
        self.tasks = tasks
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._running: dict[str, asyncio.Task[None]] = {}
        self._listeners: list[FinishedListener] = []

    def add_listener(self, listener: FinishedListener) -> None:
        """Called after every agent run with the task's resulting status."""
        self._listeners.append(listener)

    def is_running(self, task_id: str) -> bool:
        job = self._running.get(task_id)
        return job is not None and not job.done()

    def submit(self, task_id: str) -> bool:
        """Schedule a task in the background. Returns False if it is already running."""
        if self.is_running(task_id):
            return False
        job = asyncio.create_task(self._run(task_id), name=f"dotlab-task-{task_id}")
        self._running[task_id] = job
        job.add_done_callback(lambda finished: self._forget(task_id, finished))
        return True

    def _forget(self, task_id: str, job: asyncio.Task[None]) -> None:
        if self._running.get(task_id) is job:
            del self._running[task_id]

    async def _run(self, task_id: str) -> None:
        async with self._semaphore:
            try:
                status = await self.agent.run(task_id)
            except asyncio.CancelledError:
                logger.info("task run cancelled", extra={"task_id": task_id})
                raise
            except Exception:  # Agent.run already handles errors; this is a last resort
                logger.exception("runner crashed", extra={"task_id": task_id})
                return
        for listener in self._listeners:
            try:
                await listener(task_id, status)
            except Exception:
                logger.exception("task listener failed", extra={"task_id": task_id})

    async def cancel(self, task_id: str) -> None:
        job = self._running.get(task_id)
        if job and not job.done():
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)

    async def wait(self, task_id: str, timeout: float | None = None) -> None:
        """Wait for the current run of a task (used by examples and tests)."""
        job = self._running.get(task_id)
        if job:
            await asyncio.wait_for(asyncio.shield(job), timeout)

    def recover(self) -> int:
        """Re-queue tasks interrupted by a restart (PENDING/PLANNING/RUNNING)."""
        ids = self.tasks.ids_with_status(
            {TaskStatus.PENDING, TaskStatus.PLANNING, TaskStatus.RUNNING}
        )
        for task_id in ids:
            self.tasks.try_transition(
                task_id, {TaskStatus.PLANNING, TaskStatus.RUNNING}, TaskStatus.PENDING
            )
            self.tasks.add_event(task_id, EventType.TASK_RESUMED, "Re-queued after restart")
            self.submit(task_id)
        return len(ids)

    async def shutdown(self) -> None:
        jobs = [j for j in self._running.values() if not j.done()]
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
