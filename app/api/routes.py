"""Top-level API router: health, overview, tools, notifications + sub-routers."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.agent.state import ACTIVE_STATUSES, TaskStatus
from app.api import approvals, conversations, memory, monitors, tasks
from app.api.deps import get_ctx, require_api_key
from app.api.schemas import Health, Overview
from app.context import AppContext
from app.tools.notifications import Notification

public_router = APIRouter(prefix="/api", tags=["system"])
protected_router = APIRouter(prefix="/api", dependencies=[Depends(require_api_key)])


@public_router.get("/health", response_model=Health)
def health(ctx: AppContext = Depends(get_ctx)) -> Health:
    """Liveness + configuration status (never exposes secrets)."""
    return Health(
        status="ok",
        llm_configured=ctx.llm.is_configured,
        model_check=ctx.model_check,
        search_provider=ctx.settings.search_provider,
        tools=[
            {"name": t["name"], "available": t["available"] and t["permitted"]}
            for t in ctx.tools.describe()
        ],
    )


@protected_router.get("/overview", response_model=Overview, tags=["system"])
def overview(ctx: AppContext = Depends(get_ctx)) -> Overview:
    counts = ctx.tasks.status_counts()
    active = sum(counts[s] for s in ACTIVE_STATUSES)
    return Overview(
        active_tasks=active,
        completed_tasks=counts[TaskStatus.COMPLETED],
        waiting_approvals=ctx.approvals.count_pending(),
        waiting_input=counts[TaskStatus.WAITING_INPUT],
        failed_tasks=counts[TaskStatus.FAILED],
        paused_tasks=counts[TaskStatus.PAUSED],
        monitoring_jobs=sum(1 for m in ctx.monitors.list() if m.enabled),
        unread_notifications=len(ctx.notifications.list(limit=1000, unread_only=True)),
        status_counts=counts,
    )


@protected_router.get("/tools", tags=["system"])
def list_tools(ctx: AppContext = Depends(get_ctx)) -> list[dict]:
    return ctx.tools.describe()


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
protected_router.include_router(memory.router)
protected_router.include_router(conversations.router)
