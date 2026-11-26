"""Top-level API router: health, overview, tools, notifications + sub-routers."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app import __version__
from app.agent.state import ACTIVE_STATUSES, TaskStatus
from app.api import activity, approvals, conversations, memory, monitors, reports, tasks
from app.api.deps import get_ctx, require_api_key
from app.api.schemas import Health, Overview, SettingsView
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
    usage = ctx.usage.grand_total()
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
        llm_calls_total=usage.llm_calls,
        tokens_used_total=usage.total_tokens,
    )


@protected_router.get("/settings", response_model=SettingsView, tags=["system"])
def settings_view(ctx: AppContext = Depends(get_ctx)) -> SettingsView:
    """Current configuration for the dashboard. Read-only and free of secrets:
    values are changed in .env and take effect after a restart."""
    s = ctx.settings
    return SettingsView(
        version=__version__,
        openai_model=s.openai_model,
        llm_configured=ctx.llm.is_configured,
        model_check=ctx.model_check,
        api_key_protection=s.dot_lab_api_key is not None,
        search_provider=s.search_provider,
        browser_backend=s.browser_backend,
        tool_permission_level=s.tool_permission_level,
        approval_required_tools=s.approval_required_tools,
        approval_rejection_policy=s.approval_rejection_policy,
        limits={
            "MAX_AGENT_ITERATIONS": s.max_agent_iterations,
            "MAX_PLAN_STEPS": s.max_plan_steps,
            "MAX_STEP_RETRIES": s.max_step_retries,
            "MAX_REPLANS": s.max_replans,
            "TOOL_TIMEOUT_SECONDS": s.tool_timeout_seconds,
            "TASK_TIMEOUT_SECONDS": s.task_timeout_seconds,
            "MAX_CONCURRENT_TASKS": s.max_concurrent_tasks,
            "MAX_GOAL_LENGTH": s.max_goal_length,
            "CONVERSATION_CONTEXT_MESSAGES": s.conversation_context_messages,
            "CSV_MAX_BYTES": s.csv_max_bytes,
        },
        max_task_tokens=s.max_task_tokens or None,
        max_task_cost_usd=s.max_task_cost_usd,
        prices_configured=s.pricing is not None,
        monitor_scheduler_enabled=s.enable_monitor_scheduler,
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
protected_router.include_router(activity.router)
protected_router.include_router(reports.router)
