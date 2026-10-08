"""Application container: builds and wires every component once.

Both the FastAPI app and the example scripts use :func:`build_context`, so
there is exactly one place where dependencies are assembled. Tests pass a fake
LLM through the ``llm`` argument.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agent.agent import Agent
from app.agent.evaluator import Evaluator
from app.agent.llm import LLMClient, build_llm
from app.approvals.manager import ApprovalManager
from app.config import Settings
from app.database.database import Database
from app.memory.memory import MemoryStore, SQLiteMemoryStore
from app.services.monitoring import MonitorScheduler, MonitorService
from app.services.runner import TaskRunner
from app.services.task_service import TaskService
from app.tools import build_default_registry
from app.tools.base import ToolRegistry
from app.tools.notifications import NotificationStore


@dataclass
class AppContext:
    settings: Settings
    db: Database
    llm: LLMClient
    tasks: TaskService
    memory: MemoryStore
    approvals: ApprovalManager
    notifications: NotificationStore
    tools: ToolRegistry
    agent: Agent
    runner: TaskRunner
    monitors: MonitorService
    scheduler: MonitorScheduler


def build_context(
    settings: Settings,
    *,
    llm: LLMClient | None = None,
    tools: ToolRegistry | None = None,
    retry_base_delay: float = 1.0,
) -> AppContext:
    """Create all services. Must be called inside a running event loop only if
    you intend to use the runner/scheduler immediately (they create asyncio tasks
    lazily, so construction itself is loop-agnostic)."""
    db = Database(settings.database_url)
    db.create_all()
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)

    if llm is None:
        llm = build_llm(
            settings.openai_api_key.get_secret_value() if settings.openai_api_key else None,
            settings.openai_model,
            timeout=float(settings.openai_timeout_seconds),
            max_retries=settings.openai_max_retries,
        )
    tasks = TaskService(db)
    memory = SQLiteMemoryStore(db)
    approvals = ApprovalManager(db, always_require=settings.approval_required_tools)
    notifications = NotificationStore(db)
    registry = tools or build_default_registry(settings, notifications)
    agent = Agent(
        llm=llm,
        tools=registry,
        memory=memory,
        approvals=approvals,
        tasks=tasks,
        settings=settings,
        retry_base_delay=retry_base_delay,
    )
    runner = TaskRunner(agent, tasks, max_concurrent=settings.max_concurrent_tasks)
    monitors = MonitorService(db, tasks, runner, Evaluator(llm), notifications)
    scheduler = MonitorScheduler(monitors, poll_seconds=settings.monitor_poll_seconds)
    return AppContext(
        settings=settings,
        db=db,
        llm=llm,
        tasks=tasks,
        memory=memory,
        approvals=approvals,
        notifications=notifications,
        tools=registry,
        agent=agent,
        runner=runner,
        monitors=monitors,
        scheduler=scheduler,
    )
