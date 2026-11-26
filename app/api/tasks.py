"""Task endpoints: create, list, inspect, answer, report, pause, resume, cancel."""

from __future__ import annotations

import contextlib

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.agent.state import EventType, TaskStatus
from app.api.deps import get_ctx
from app.api.schemas import (
    ClarificationAnswer,
    TaskCreate,
    TaskCreated,
    TaskDetail,
    TaskSummary,
)
from app.context import AppContext
from app.database.database import utcnow
from app.services.report_service import build_task_report
from app.services.task_service import InvalidTransitionError, TaskNotFoundError, TaskView

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _get(ctx: AppContext, task_id: str) -> TaskView:
    try:
        return ctx.tasks.get_task(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found") from exc


def check_can_create_task(ctx: AppContext, goal: str) -> None:
    """Shared checks for every way of starting a task (tasks API, conversations)."""
    if len(goal) > ctx.settings.max_goal_length:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Goal is longer than MAX_GOAL_LENGTH ({ctx.settings.max_goal_length})",
        )
    if not ctx.llm.is_configured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "OpenAI is not configured. Set OPENAI_API_KEY and OPENAI_MODEL and restart.",
        )


@router.post("", response_model=TaskCreated, status_code=status.HTTP_201_CREATED)
async def create_task(body: TaskCreate, ctx: AppContext = Depends(get_ctx)) -> TaskCreated:
    """Create a task from a goal and start it in the background."""
    check_can_create_task(ctx, body.goal)
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
        clarifications=task.clarifications,
        iterations=task.iterations,
        result=task.result,
        error=task.error,
        events=ctx.tasks.list_events(task_id),
        tool_calls=ctx.tasks.list_tool_calls(task_id),
        approvals=ctx.approvals.list(task_id=task_id),
        memory=ctx.memory.retrieve(task_id=task_id, limit=100),
        usage=ctx.agent.budget_for(task_id).report(),
    )


@router.post("/{task_id}/answer", response_model=TaskSummary)
async def answer_task(
    task_id: str, body: ClarificationAnswer, ctx: AppContext = Depends(get_ctx)
) -> TaskSummary:
    """Answer the question a task asked (status WAITING_INPUT) and continue it."""
    _get(ctx, task_id)
    try:
        task = answer_and_resume(ctx, task_id, body.answer)
    except InvalidTransitionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return TaskSummary.from_view(task)


def answer_and_resume(ctx: AppContext, task_id: str, answer: str) -> TaskView:
    """Shared by the tasks and conversations APIs. Must run on the event loop."""
    task = ctx.tasks.answer_clarification(task_id, answer)
    ctx.runner.submit(task_id)
    return task


@router.get("/{task_id}/report", response_class=Response)
def task_report(
    task_id: str, download: bool = True, ctx: AppContext = Depends(get_ctx)
) -> Response:
    """The task as a Markdown report (attachment by default; ``download=false`` to view)."""
    task = _get(ctx, task_id)
    text = build_task_report(
        task,
        events=ctx.tasks.list_events(task_id),
        tool_calls=ctx.tasks.list_tool_calls(task_id),
        approvals=ctx.approvals.list(task_id=task_id),
        usage=ctx.agent.budget_for(task_id).report(),
        generated_at=utcnow(),
    )
    disposition = "attachment" if download else "inline"
    return Response(
        content=text,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'{disposition}; filename="dot-lab-task-{task_id[:8]}.md"'},
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
    if task.conversation_id:
        with contextlib.suppress(LookupError):  # conversation may have been deleted
            ctx.conversations.add_message(
                task.conversation_id, "assistant", "The task was cancelled.", task_id=task_id
            )
    return TaskSummary.from_view(task)
