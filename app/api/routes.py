"""Top-level API router: health, overview, tools, memory, notifications + sub-routers."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.agent.state import TaskStatus
from app.api import approvals, monitors, tasks
from app.api.deps import get_ctx, require_api_key
from app.api.schemas import Health, Overview
from app.context import AppContext
from app.memory.models import MemoryRecord
from app.tools.notifications import Notification

public_router = APIRouter(prefix="/api", tags=["system"])
protected_router = APIRouter(prefix="/api", dependencies=[Depends(require_api_key)])


@public_router.get("/health", response_model=Health)
def health(ctx: AppContext = Depends(get_ctx)) -> Health:
    """Liveness + configuration status (never exposes secrets)."""
    return Health(
        status="ok",
        llm_configured=ctx.llm.is_configured,
        search_provider=ctx.settings.search_provider,
        tools=[{"name": t.name, "available": t.availability().available} for t in ctx.tools.all()],
    )


@protected_router.get("/overview", response_model=Overview, tags=["system"])
def overview(ctx: AppContext = Depends(get_ctx)) -> Overview:
    counts = ctx.tasks.status_counts()
    active = sum(counts[s] for s in (TaskStatus.PENDING, TaskStatus.PLANNING, TaskStatus.RUNNING))
    return Overview(
        active_tasks=active,
        completed_tasks=counts[TaskStatus.COMPLETED],
        waiting_approvals=ctx.approvals.count_pending(),
        failed_tasks=counts[TaskStatus.FAILED],
        paused_tasks=counts[TaskStatus.PAUSED],
        monitoring_jobs=sum(1 for m in ctx.monitors.list() if m.enabled),
        unread_notifications=len(ctx.notifications.list(limit=1000, unread_only=True)),
        status_counts=counts,
    )


@protected_router.get("/tools", tags=["system"])
def list_tools(ctx: AppContext = Depends(get_ctx)) -> list[dict]:
    return [t.describe() for t in ctx.tools.all()]


@protected_router.get("/memory/search", response_model=list[MemoryRecord], tags=["memory"])
def search_memory(
    q: str = Query(min_length=2, max_length=500),
    limit: int = Query(default=10, ge=1, le=50),
    ctx: AppContext = Depends(get_ctx),
) -> list[MemoryRecord]:
    return ctx.memory.search(q, limit=limit)


@protected_router.delete(
    "/memory/{memory_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["memory"]
)
def delete_memory(memory_id: int, ctx: AppContext = Depends(get_ctx)) -> None:
    if not ctx.memory.delete(memory_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Memory not found")


@protected_router.get("/notifications", response_model=list[Notification], tags=["notifications"])
def list_notifications(
    unread_only: bool = False, ctx: AppContext = Depends(get_ctx)
) -> list[Notification]:
    return ctx.notifications.list(unread_only=unread_only)


@protected_router.post("/notifications/{notification_id}/read", tags=["notifications"])
def mark_read(notification_id: int, ctx: AppContext = Depends(get_ctx)) -> dict[str, bool]:
    if not ctx.notifications.mark_read(notification_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notification not found")
    return {"ok": True}


protected_router.include_router(tasks.router)
protected_router.include_router(approvals.router)
protected_router.include_router(monitors.router)
