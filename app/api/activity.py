"""Activity feed: the newest events across all tasks."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query

from app.agent.state import EventType
from app.api.deps import get_ctx
from app.context import AppContext
from app.services.task_service import ActivityEvent

router = APIRouter(prefix="/activity", tags=["activity"])

# Filters offered by the dashboard, mapped to the events they include.
ACTIVITY_FILTERS: dict[str, set[str]] = {
    "errors": {
        EventType.TOOL_FAILED,
        EventType.STEP_FAILED,
        EventType.TASK_FAILED,
        EventType.APPROVAL_REJECTED,
    },
    "approvals": {
        EventType.APPROVAL_REQUESTED,
        EventType.APPROVAL_GRANTED,
        EventType.APPROVAL_REJECTED,
    },
    "questions": {EventType.CLARIFICATION_REQUESTED, EventType.CLARIFICATION_ANSWERED},
    "tools": {EventType.TOOL_CALLED, EventType.TOOL_FAILED},
    "tasks": {
        EventType.TASK_CREATED,
        EventType.TASK_COMPLETED,
        EventType.TASK_FAILED,
        EventType.TASK_CANCELLED,
        EventType.TASK_PAUSED,
        EventType.TASK_RESUMED,
    },
}


@router.get("", response_model=list[ActivityEvent])
def activity(
    kind: Literal["all", "errors", "approvals", "questions", "tools", "tasks"] = "all",
    limit: int = Query(default=50, ge=1, le=200),
    before_id: int | None = Query(default=None, ge=1),
    ctx: AppContext = Depends(get_ctx),
) -> list[ActivityEvent]:
    """Newest first. Pass the last ``id`` you received as ``before_id`` to load older events."""
    events = None if kind == "all" else {e.value for e in ACTIVITY_FILTERS[kind]}
    return ctx.tasks.recent_events(limit=limit, before_id=before_id, events=events)
