"""Approval endpoints (human-in-the-loop)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.agent.state import TaskStatus
from app.api.deps import get_ctx
from app.api.schemas import ApprovalDecision
from app.approvals.manager import Approval, ApprovalError, ApprovalStatus
from app.context import AppContext

router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get("", response_model=list[Approval])
def list_approvals(
    status_filter: ApprovalStatus | None = Query(default=ApprovalStatus.PENDING, alias="status"),
    task_id: str | None = None,
    ctx: AppContext = Depends(get_ctx),
) -> list[Approval]:
    return ctx.approvals.list(status=status_filter, task_id=task_id)


def _decide(ctx: AppContext, approval_id: str, approved: bool, note: str | None) -> Approval:
    try:
        approval = ctx.approvals.decide(approval_id, approved=approved, note=note)
    except ApprovalError as exc:
        code = status.HTTP_404_NOT_FOUND if "not found" in str(exc) else status.HTTP_409_CONFLICT
        raise HTTPException(code, str(exc)) from exc
    # Re-queue the task so the agent picks up the decision.
    if ctx.tasks.try_transition(
        approval.task_id, {TaskStatus.WAITING_APPROVAL}, TaskStatus.PENDING
    ):
        ctx.runner.submit(approval.task_id)
    return approval


@router.post("/{approval_id}/approve", response_model=Approval)
async def approve(
    approval_id: str, body: ApprovalDecision | None = None, ctx: AppContext = Depends(get_ctx)
) -> Approval:
    return _decide(ctx, approval_id, True, body.note if body else None)


@router.post("/{approval_id}/reject", response_model=Approval)
async def reject(
    approval_id: str, body: ApprovalDecision | None = None, ctx: AppContext = Depends(get_ctx)
) -> Approval:
    return _decide(ctx, approval_id, False, body.note if body else None)
