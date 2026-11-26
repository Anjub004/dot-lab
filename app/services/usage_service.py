"""Persistence for LLM token usage (one row per LLM call in ``llm_usage``)."""

from __future__ import annotations

from sqlalchemy import func, select

from app.agent.usage import Usage
from app.database.database import Database
from app.database.models import LlmUsageRow


class UsageService:
    """Stores per-call usage and returns per-task totals."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def record(
        self, *, task_id: str, model: str, purpose: str, input_tokens: int, output_tokens: int
    ) -> None:
        with self._db.session() as session:
            session.add(
                LlmUsageRow(
                    task_id=task_id,
                    model=model[:100],
                    purpose=purpose[:64],
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
            )

    def grand_total(self) -> Usage:
        """Usage of all tasks together."""
        return self._sum(None)

    def totals(self, task_id: str) -> Usage:
        return self._sum(task_id)

    def _sum(self, task_id: str | None) -> Usage:
        stmt = select(
            func.count(LlmUsageRow.id),
            func.coalesce(func.sum(LlmUsageRow.input_tokens), 0),
            func.coalesce(func.sum(LlmUsageRow.output_tokens), 0),
        )
        if task_id is not None:
            stmt = stmt.where(LlmUsageRow.task_id == task_id)
        with self._db.session() as session:
            calls, input_tokens, output_tokens = session.execute(stmt).one()
        return Usage(
            llm_calls=int(calls), input_tokens=int(input_tokens), output_tokens=int(output_tokens)
        )
