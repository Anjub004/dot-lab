"""Task endpoints: create, list, inspect, pause, resume, cancel."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.agent.state import EventType, TaskStatus
from app.api.deps import get_ctx
from app.api.schemas import TaskCreate, TaskCreated, TaskDetail, TaskSummary
from app.context import AppContext
from app.services.task_service import InvalidTransitionError, TaskNotFoundError, TaskView

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _get(ctx: AppContext, task_id: str) -> TaskView:
    try:
        return ctx.tasks.get_task(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found") from exc


@router.post("", response_model=TaskCreated, status_code=status.HTTP_201_CREATED)
async def create_task(body: TaskCreate, ctx: AppContext = Depends(get_ctx)) -> TaskCreated:
    """Create a task from a goal and start it in the background."""
    if len(body.goal) > ctx.settings.max_goal_length:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Goal is longer than MAX_GOAL_LENGTH ({ctx.settings.max_goal_length})",
        )
    if not ctx.llm.is_configured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "OpenAI is not configured. Set OPENAI_API_KEY and OPENAI_MODEL and restart.",
        )
    task = ctx.tasks.create_task(body.goal)
    ctx.runner.submit(task.id)
    return TaskCreated(
        task_id=task.id, goal=task.goal, status=task.status, created_at=task.created_at
    )


@router.get("", response_model=list[TaskSummary])
def list_tasks(
    status_filter: TaskStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    ctx: AppContext = Depends(get_ctx),
) -> list[TaskSummary]:
    return [TaskSummary.from_view(t) for t in ctx.tasks.list_tasks(status_filter, limit)]


@router.get("/{task_id}", response_model=TaskDetail)
def get_task(task_id: str, ctx: AppContext = Depends(get_ctx)) -> TaskDetail:
    task = _get(ctx, task_id)
    summary = TaskSummary.from_view(task)
    return TaskDetail(
        **summary.model_dump(),
        plan=task.plan,
        iterations=task.iterations,
        result=task.result,
        error=task.error,
        events=ctx.tasks.list_events(task_id),
        tool_calls=ctx.tasks.list_tool_calls(task_id),
        approvals=ctx.approvals.list(task_id=task_id),
        memory=ctx.memory.retrieve(task_id=task_id, limit=100),
    )


@router.post("/{task_id}/pause", response_model=TaskSummary)
def pause_task(task_id: str, ctx: AppContext = Depends(get_ctx)) -> TaskSummary:
    """Pause after the current step finishes."""
    _get(ctx, task_id)
    try:
        task = ctx.tasks.transition(task_id, TaskStatus.PAUSED)
    except InvalidTransitionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    ctx.tasks.add_event(task_id, EventType.TASK_PAUSED, "Paused by user")
    return TaskSummary.from_view(task)


@router.post("/{task_id}/resume", response_model=TaskSummary)
def resume_task(task_id: str, ctx: AppContext = Depends(get_ctx)) -> TaskSummary:
    current = _get(ctx, task_id)
    if current.status is not TaskStatus.PAUSED:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Task is {current.status}, not PAUSED")
    task = ctx.tasks.transition(task_id, TaskStatus.PENDING)
    ctx.tasks.add_event(task_id, EventType.TASK_RESUMED, "Resumed by user")
    ctx.runner.submit(task_id)
    return TaskSummary.from_view(task)


@router.post("/{task_id}/cancel", response_model=TaskSummary)
async def cancel_task(task_id: str, ctx: AppContext = Depends(get_ctx)) -> TaskSummary:
    _get(ctx, task_id)
    try:
        task = ctx.tasks.transition(task_id, TaskStatus.CANCELLED)
    except InvalidTransitionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    ctx.approvals.cancel_for_task(task_id)
    ctx.tasks.add_event(task_id, EventType.TASK_CANCELLED, "Cancelled by user")
    await ctx.runner.cancel(task_id)
    return TaskSummary.from_view(task)
