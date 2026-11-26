"""Token and cost budgets for LLM calls.

How it works:

* ``Agent.run`` creates a :class:`TaskBudget` for the task and activates it with
  :func:`use_budget`. The budget lives in a :class:`~contextvars.ContextVar`, so
  every LLM call made while the task runs (planner, executor, reviewer) sees it
  without having to pass it through every function.
* Before each call, :meth:`TaskBudget.check` raises :class:`BudgetExceededError`
  if the task has already used its allowance.
* After each call, the LLM client reports the tokens the API returned with
  :meth:`TaskBudget.record`, which saves one row per call in ``llm_usage``.

Totals are loaded from the database when a run starts, so the budget covers the
whole task, including runs resumed after an approval, a pause or a restart.

Costs are estimates calculated from the prices you configure; Dot Lab does not
know your actual OpenAI bill.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel


class BudgetExceededError(RuntimeError):
    """The task used its token or cost allowance; no further LLM calls are made."""


@dataclass
class Usage:
    """Accumulated LLM usage."""

    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class UsageReport(BaseModel):
    """Usage and budget of one task, as shown in the API and dashboard."""

    llm_calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost_usd: float | None
    max_tokens: int | None
    max_cost_usd: float | None


class UsageSink(Protocol):
    """Where per-call usage is stored (``UsageService`` in production)."""

    def record(
        self, *, task_id: str, model: str, purpose: str, input_tokens: int, output_tokens: int
    ) -> None: ...


@dataclass(frozen=True)
class Pricing:
    """Prices in USD per one million tokens (from your OpenAI pricing page)."""

    input_per_1m: float
    output_per_1m: float

    def cost(self, usage: Usage) -> float:
        return (
            usage.input_tokens * self.input_per_1m + usage.output_tokens * self.output_per_1m
        ) / 1_000_000


class TaskBudget:
    """Tracks one task's LLM usage and enforces its limits."""

    def __init__(
        self,
        *,
        task_id: str,
        sink: UsageSink,
        max_tokens: int | None = None,
        max_cost_usd: float | None = None,
        pricing: Pricing | None = None,
        initial: Usage | None = None,
    ) -> None:
        if max_cost_usd is not None and pricing is None:
            raise ValueError("A cost limit needs token prices to estimate cost")
        self.task_id = task_id
        self.sink = sink
        self.max_tokens = max_tokens or None  # 0 means unlimited
        self.max_cost_usd = max_cost_usd
        self.pricing = pricing
        self.usage = initial or Usage()

    @property
    def estimated_cost_usd(self) -> float | None:
        return self.pricing.cost(self.usage) if self.pricing else None

    def check(self) -> None:
        """Raise BudgetExceededError if another LLM call is not allowed."""
        if self.max_tokens is not None and self.usage.total_tokens >= self.max_tokens:
            raise BudgetExceededError(
                f"Token budget exceeded: used {self.usage.total_tokens:,} of "
                f"{self.max_tokens:,} tokens (MAX_TASK_TOKENS)."
            )
        cost = self.estimated_cost_usd
        if self.max_cost_usd is not None and cost is not None and cost >= self.max_cost_usd:
            raise BudgetExceededError(
                f"Cost budget exceeded: estimated ${cost:.4f} of "
                f"${self.max_cost_usd:.4f} (MAX_TASK_COST_USD)."
            )

    def record(self, *, model: str, purpose: str, input_tokens: int, output_tokens: int) -> None:
        """Add the tokens of one completed call and persist them."""
        input_tokens, output_tokens = max(0, input_tokens), max(0, output_tokens)
        self.usage.llm_calls += 1
        self.usage.input_tokens += input_tokens
        self.usage.output_tokens += output_tokens
        self.sink.record(
            task_id=self.task_id,
            model=model,
            purpose=purpose,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def report(self) -> UsageReport:
        return build_report(self.usage, self.max_tokens, self.max_cost_usd, self.pricing)


def build_report(
    usage: Usage,
    max_tokens: int | None,
    max_cost_usd: float | None,
    pricing: Pricing | None,
) -> UsageReport:
    return UsageReport(
        llm_calls=usage.llm_calls,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
        estimated_cost_usd=round(pricing.cost(usage), 6) if pricing else None,
        max_tokens=max_tokens or None,
        max_cost_usd=max_cost_usd,
    )


_current_budget: ContextVar[TaskBudget | None] = ContextVar("dot_lab_task_budget", default=None)


def current_budget() -> TaskBudget | None:
    """The budget of the task whose code is running, or None outside a task."""
    return _current_budget.get()


@contextmanager
def use_budget(budget: TaskBudget) -> Iterator[TaskBudget]:
    """Activate ``budget`` for every LLM call made inside this block."""
    token = _current_budget.set(budget)
    try:
        yield budget
    finally:
        _current_budget.reset(token)
