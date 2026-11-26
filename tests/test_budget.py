"""Token and cost budget tests (offline)."""

from __future__ import annotations

import pytest

from app.agent.evaluator import GoalEvaluation, StepEvaluation
from app.agent.executor import ToolArguments
from app.agent.planner import PlannerOutput
from app.agent.state import TaskStatus
from app.agent.usage import BudgetExceededError, Pricing, TaskBudget, Usage
from app.config import Settings
from app.context import AppContext, build_context
from tests.conftest import ScriptedLLM, args, goal_done, plan, step_ok


class ListSink:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def record(self, **row: object) -> None:
        self.rows.append(row)


def script_two_steps(llm: ScriptedLLM) -> None:
    llm.add(
        PlannerOutput,
        plan(("Compute 6*7", "calculator", False), ("Compute 2*3", "calculator", False)),
    )
    llm.add(ToolArguments, args('{"expression": "6*7"}'), args('{"expression": "2*3"}'))
    llm.add(StepEvaluation, step_ok())
    llm.add(GoalEvaluation, goal_done("42 and 6"))


# ----------------------------------------------------------------- unit --


def test_budget_counts_tokens_and_blocks_further_calls() -> None:
    sink = ListSink()
    budget = TaskBudget(task_id="t1", sink=sink, max_tokens=1000)
    budget.check()  # nothing used yet
    budget.record(model="m", purpose="PlannerOutput", input_tokens=700, output_tokens=300)

    assert budget.usage == Usage(llm_calls=1, input_tokens=700, output_tokens=300)
    assert sink.rows[0]["purpose"] == "PlannerOutput" and sink.rows[0]["task_id"] == "t1"
    with pytest.raises(BudgetExceededError, match="Token budget exceeded"):
        budget.check()


def test_zero_token_limit_means_unlimited() -> None:
    budget = TaskBudget(task_id="t", sink=ListSink(), max_tokens=0)
    budget.record(model="m", purpose="p", input_tokens=10**9, output_tokens=0)
    budget.check()
    assert budget.report().max_tokens is None


def test_cost_estimate_and_cost_limit() -> None:
    pricing = Pricing(input_per_1m=2.0, output_per_1m=10.0)
    budget = TaskBudget(task_id="t", sink=ListSink(), max_cost_usd=0.01, pricing=pricing)
    budget.record(model="m", purpose="p", input_tokens=1000, output_tokens=500)
    # 1000 * 2/1M + 500 * 10/1M = 0.002 + 0.005
    assert budget.estimated_cost_usd == pytest.approx(0.007)
    budget.check()
    budget.record(model="m", purpose="p", input_tokens=1000, output_tokens=500)
    with pytest.raises(BudgetExceededError, match="Cost budget exceeded"):
        budget.check()


def test_cost_limit_requires_prices() -> None:
    with pytest.raises(ValueError):
        TaskBudget(task_id="t", sink=ListSink(), max_cost_usd=1.0)
    with pytest.raises(ValueError, match="MAX_TASK_COST_USD"):
        Settings(max_task_cost_usd=1.0)
    settings = Settings(
        max_task_cost_usd=1.0, openai_input_cost_per_1m=1.0, openai_output_cost_per_1m=4.0
    )
    assert settings.pricing == Pricing(1.0, 4.0)


def test_settings_read_budget_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_TASK_TOKENS", "5000")
    monkeypatch.setenv("MAX_TASK_COST_USD", "0.5")
    monkeypatch.setenv("OPENAI_INPUT_COST_PER_1M", "1.25")
    monkeypatch.setenv("OPENAI_OUTPUT_COST_PER_1M", "10")
    settings = Settings.from_env(env_file="/nonexistent.env")
    assert settings.max_task_tokens == 5000
    assert settings.max_task_cost_usd == 0.5
    assert settings.pricing == Pricing(1.25, 10.0)


# ------------------------------------------------------------ agent runs --


async def test_task_records_usage(ctx: AppContext, llm: ScriptedLLM) -> None:
    llm.usage_per_call = (100, 20)
    script_two_steps(llm)
    task = ctx.tasks.create_task("Compute two products")

    assert await ctx.agent.run(task.id) is TaskStatus.COMPLETED
    totals = ctx.usage.totals(task.id)
    # planner + 2x(args + step review) + final review = 6 calls
    assert totals == Usage(llm_calls=6, input_tokens=600, output_tokens=120)
    report = ctx.agent.budget_for(task.id).report()
    assert report.total_tokens == 720 and report.max_tokens == ctx.settings.max_task_tokens
    assert report.estimated_cost_usd is None  # no prices configured


async def test_task_fails_cleanly_when_token_budget_is_exceeded(
    settings: Settings, llm: ScriptedLLM
) -> None:
    settings = settings.model_copy(update={"max_task_tokens": 250})
    context = build_context(settings, llm=llm, retry_base_delay=0)
    try:
        llm.usage_per_call = (100, 20)  # 120 tokens per call
        script_two_steps(llm)
        task = context.tasks.create_task("Compute two products")

        assert await context.agent.run(task.id) is TaskStatus.FAILED
        final = context.tasks.get_task(task.id)
        assert final.error and "Token budget exceeded" in final.error
        assert "MAX_TASK_TOKENS" in final.error
        # A call is allowed while usage is under the limit, so calls 1-3 ran
        # (120 -> 240 -> 360 tokens) and call 4 was blocked before it was made.
        # The limit can therefore be exceeded by at most one call.
        assert len(llm.calls) == 3
        assert context.usage.totals(task.id) == Usage(3, 300, 60)
        assert "used 360 of 250" in final.error
        assert [e.event for e in context.tasks.list_events(task.id)][-1] == "TASK_FAILED"
    finally:
        context.db.dispose()


async def test_budget_covers_resumed_runs(settings: Settings, llm: ScriptedLLM) -> None:
    """Usage from earlier runs counts: a resumed task cannot reset its budget."""
    settings = settings.model_copy(update={"max_task_tokens": 1000})
    context = build_context(settings, llm=llm, retry_base_delay=0)
    try:
        task = context.tasks.create_task("Earlier run already used the budget")
        context.usage.record(
            task_id=task.id, model="m", purpose="PlannerOutput", input_tokens=900, output_tokens=100
        )
        script_two_steps(llm)

        assert await context.agent.run(task.id) is TaskStatus.FAILED
        assert llm.calls == []  # no LLM call was made at all
        assert "Token budget exceeded" in (context.tasks.get_task(task.id).error or "")
    finally:
        context.db.dispose()
