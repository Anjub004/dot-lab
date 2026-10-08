"""Human-in-the-loop approval management.

Flow:

1. The agent asks :meth:`ApprovalManager.request` before a sensitive step.
   The task moves to ``WAITING_APPROVAL`` and the agent loop exits.
2. A human calls :meth:`decide` (via the API/dashboard) to approve or reject.
3. The task is re-queued. When the agent reaches the step again it calls
   :meth:`active_for_step`, uses the decision, and marks it ``consumed`` so an
   approval can never be reused for a different action.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select

from app.database.database import Database, utcnow
from app.database.models import ApprovalRow


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class ApprovalError(RuntimeError):
    """Invalid approval operation (unknown id, already decided, ...)."""


class Approval(BaseModel):
    id: str
    task_id: str
    step_id: str
    tool: str | None
    action: str
    reason: str
    explanation: str
    input: dict[str, Any]
    status: ApprovalStatus
    decision_note: str | None
    consumed: bool
    created_at: datetime
    decided_at: datetime | None


def _to_model(row: ApprovalRow) -> Approval:
    return Approval(
        id=row.id,
        task_id=row.task_id,
        step_id=row.step_id,
        tool=row.tool,
        action=row.action,
        reason=row.reason,
        explanation=row.explanation,
        input=json.loads(row.input_json),
        status=ApprovalStatus(row.status),
        decision_note=row.decision_note,
        consumed=row.consumed,
        created_at=row.created_at,
        decided_at=row.decided_at,
    )


class ApprovalManager:
    """Creates, lists and resolves approval requests."""

    def __init__(self, db: Database, always_require: list[str] | None = None) -> None:
        self._db = db
        self.always_require = {t.lower() for t in always_require or []}

    def policy_reason(
        self, *, tool_name: str | None, step_requires_approval: bool, tool_reason: str | None
    ) -> str | None:
        """Combine the three approval sources into a single reason (or None)."""
        if tool_reason:
            return tool_reason
        if tool_name and tool_name.lower() in self.always_require:
            return f"Tool '{tool_name}' is configured to always require approval."
        if step_requires_approval:
            return "The plan marked this step as sensitive."
        return None

    def request(
        self,
        *,
        task_id: str,
        step_id: str,
        tool: str | None,
        action: str,
        reason: str,
        explanation: str,
        tool_input: dict[str, Any],
    ) -> Approval:
        row = ApprovalRow(
            task_id=task_id,
            step_id=step_id,
            tool=tool,
            action=action,
            reason=reason,
            explanation=explanation,
            input_json=json.dumps(tool_input, default=str),
            status=ApprovalStatus.PENDING.value,
        )
        with self._db.session() as session:
            session.add(row)
            session.flush()
            return _to_model(row)

    def get(self, approval_id: str) -> Approval:
        with self._db.session() as session:
            row = session.get(ApprovalRow, approval_id)
            if row is None:
                raise ApprovalError(f"Approval '{approval_id}' not found")
            return _to_model(row)

    def list(
        self, *, status: ApprovalStatus | None = None, task_id: str | None = None, limit: int = 100
    ) -> list[Approval]:
        stmt = select(ApprovalRow).order_by(ApprovalRow.created_at.desc()).limit(limit)
        if status is not None:
            stmt = stmt.where(ApprovalRow.status == status.value)
        if task_id is not None:
            stmt = stmt.where(ApprovalRow.task_id == task_id)
        with self._db.session() as session:
            return [_to_model(r) for r in session.scalars(stmt)]

    def active_for_step(self, task_id: str, step_id: str) -> Approval | None:
        """Latest unconsumed approval for a step, if any."""
        stmt = (
            select(ApprovalRow)
            .where(
                ApprovalRow.task_id == task_id,
                ApprovalRow.step_id == step_id,
                ApprovalRow.consumed.is_(False),
                ApprovalRow.status != ApprovalStatus.CANCELLED.value,
            )
            .order_by(ApprovalRow.created_at.desc())
            .limit(1)
        )
        with self._db.session() as session:
            row = session.scalars(stmt).first()
            return _to_model(row) if row else None

    def decide(self, approval_id: str, *, approved: bool, note: str | None = None) -> Approval:
        with self._db.session() as session:
            row = session.get(ApprovalRow, approval_id)
            if row is None:
                raise ApprovalError(f"Approval '{approval_id}' not found")
            if row.status != ApprovalStatus.PENDING.value:
                raise ApprovalError(f"Approval is already {row.status}")
            row.status = (ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED).value
            row.decision_note = note
            row.decided_at = utcnow()
            return _to_model(row)

    def consume(self, approval_id: str) -> None:
        with self._db.session() as session:
            row = session.get(ApprovalRow, approval_id)
            if row is not None:
                row.consumed = True

    def cancel_for_task(self, task_id: str) -> int:
        """Cancel all pending approvals of a task (e.g. when it is cancelled)."""
        with self._db.session() as session:
            rows = session.scalars(
                select(ApprovalRow).where(
                    ApprovalRow.task_id == task_id,
                    ApprovalRow.status == ApprovalStatus.PENDING.value,
                )
            ).all()
            for row in rows:
                row.status = ApprovalStatus.CANCELLED.value
                row.decided_at = utcnow()
            return len(rows)

    def count_pending(self) -> int:
        stmt = select(func.count(ApprovalRow.id)).where(
            ApprovalRow.status == ApprovalStatus.PENDING.value
        )
        with self._db.session() as session:
            return int(session.scalar(stmt) or 0)
