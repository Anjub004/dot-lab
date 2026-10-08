"""Monitoring-mode endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_ctx
from app.api.schemas import MonitorCreate
from app.context import AppContext
from app.services.monitoring import Monitor, MonitorNotFoundError

router = APIRouter(prefix="/monitors", tags=["monitors"])


def _not_found(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Monitor not found")


@router.get("", response_model=list[Monitor])
def list_monitors(ctx: AppContext = Depends(get_ctx)) -> list[Monitor]:
    return ctx.monitors.list()


@router.post("", response_model=Monitor, status_code=status.HTTP_201_CREATED)
def create_monitor(body: MonitorCreate, ctx: AppContext = Depends(get_ctx)) -> Monitor:
    minimum = ctx.settings.monitor_min_interval_minutes
    if body.interval_minutes < minimum:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"interval_minutes must be at least {minimum} (MONITOR_MIN_INTERVAL_MINUTES)",
        )
    if not ctx.llm.is_configured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "OpenAI is not configured. Set OPENAI_API_KEY and OPENAI_MODEL and restart.",
        )
    return ctx.monitors.create(body.name, body.goal, body.interval_minutes, body.run_now)


@router.post("/{monitor_id}/run")
async def run_monitor(monitor_id: str, ctx: AppContext = Depends(get_ctx)) -> dict[str, str | None]:
    """Run a monitor cycle immediately."""
    try:
        task_id = ctx.monitors.trigger(monitor_id)
    except MonitorNotFoundError as exc:
        raise _not_found(exc) from exc
    if task_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "A previous run is still in progress")
    return {"task_id": task_id}


@router.post("/{monitor_id}/enable", response_model=Monitor)
def enable_monitor(monitor_id: str, ctx: AppContext = Depends(get_ctx)) -> Monitor:
    try:
        return ctx.monitors.set_enabled(monitor_id, True)
    except MonitorNotFoundError as exc:
        raise _not_found(exc) from exc


@router.post("/{monitor_id}/disable", response_model=Monitor)
def disable_monitor(monitor_id: str, ctx: AppContext = Depends(get_ctx)) -> Monitor:
    try:
        return ctx.monitors.set_enabled(monitor_id, False)
    except MonitorNotFoundError as exc:
        raise _not_found(exc) from exc


@router.delete("/{monitor_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_monitor(monitor_id: str, ctx: AppContext = Depends(get_ctx)) -> None:
    try:
        ctx.monitors.delete(monitor_id)
    except MonitorNotFoundError as exc:
        raise _not_found(exc) from exc
