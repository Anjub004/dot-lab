"""Agent loop, state machine and approval-flow tests (offline, scripted LLM)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.agent.evaluator import GoalEvaluation, StepEvaluation
from app.agent.executor import ReasoningOutput, ToolArguments
from app.agent.llm import LLMError, UnconfiguredLLM
from app.agent.planner import PlannedStep, PlannerOutput
from app.agent.state import (
    AgentState,
    Plan,
    PlanStep,
    StepStatus,
    TaskStatus,
    can_transition,
)
from app.approvals.manager import ApprovalStatus
from app.context import AppContext, build_context
from app.memory.models import MemoryKind
from app.tools.notifications import WebhookSender
from tests.conftest import ScriptedLLM, args, goal_done, plan, step_ok

# --------------------------------------------------------------- state --


def test_agent_state() -> None:
    p = Plan(
        goal="g",
        steps=[
            PlanStep(id="step_1", description="a", tool="calculator"),
            PlanStep(id="step_2", description="b"),
        ],
    )
    state = AgentState(task_id="t", goal="g", status=TaskStatus.PENDING, plan=None)
    assert state.needs_planning and not state.is_terminal

    assert p.next_step() is not None and p.next_step().id == "step_1"
    p.steps[0].status = StepStatus.COMPLETED
    assert p.next_step().id == "step_2"
    assert p.progress() == (1, 2)
    p.skip_remaining("done early")
    assert p.next_step() is None and p.progress() == (2, 2)
    assert p.next_step_id() == "step_3"


def test_status_transitions() -> None:
    assert can_transition(TaskStatus.PENDING, TaskStatus.PLANNING)
    assert can_transition(TaskStatus.RUNNING, TaskStatus.WAITING_APPROVAL)
    assert can_transition(TaskStatus.WAITING_APPROVAL, TaskStatus.PENDING)
    assert not can_transition(TaskStatus.COMPLETED, TaskStatus.RUNNING)
    assert not can_transition(TaskStatus.CANCELLED, TaskStatus.PENDING)
    assert not can_transition(TaskStatus.PAUSED, TaskStatus.RUNNING)


# ----------------------------------------------------------- full runs --


async def test_task_status_completes_multi_step_goal(ctx: AppContext, llm: ScriptedLLM) -> None:
    llm.add(
        PlannerOutput,
        plan(
            ("Add the numbers", "calculator", False),
            ("Save the result", "filesystem", False),
        ),
    )
    llm.add(
        ToolArguments,
        args('{"expression": "sum([10, 20, 12])"}'),
        args('{"operation": "write", "path": "results/sum.txt", "content": "42"}'),
    )
    llm.add(StepEvaluation, step_ok(observations=["The sum is 42"]))
    llm.add(GoalEvaluation, goal_done("The sum is 42 and was saved to results/sum.txt"))

    task = ctx.tasks.create_task("Calculate 10+20+12 and save the result")
    assert task.status is TaskStatus.PENDING

    status = await ctx.agent.run(task.id)

    assert status is TaskStatus.COMPLETED
    final = ctx.tasks.get_task(task.id)
    assert final.result and "42" in final.result
    assert final.completed_at is not None
    assert all(s.status is StepStatus.COMPLETED for s in final.plan.steps)
    assert (ctx.settings.workspace_dir / "results" / "sum.txt").read_text() == "42"

    calls = ctx.tasks.list_tool_calls(task.id)
    assert [c.tool for c in calls] == ["calculator", "filesystem"]
    assert all(c.status == "SUCCESS" for c in calls)

    kinds = {m.kind for m in ctx.memory.retrieve(task_id=task.id)}
    assert {
        MemoryKind.GOAL,
        MemoryKind.PLAN,
        MemoryKind.TOOL_RESULT,
        MemoryKind.OBSERVATION,
        MemoryKind.RESULT,
    } <= kinds
    events = [e.event for e in ctx.tasks.list_events(task.id)]
    assert events[0] == "TASK_CREATED" and events[-1] == "TASK_COMPLETED"


async def test_reasoning_step_and_replanning(ctx: AppContext, llm: ScriptedLLM) -> None:
    llm.add(PlannerOutput, plan(("Write a short summary", None, False)))
    llm.add(ReasoningOutput, ReasoningOutput(output="Draft summary"))
    llm.add(StepEvaluation, step_ok())
    llm.add(
        GoalEvaluation,
        GoalEvaluation(
            goal_satisfied=False,
            final_result="partial",
            reason="needs a number",
            additional_steps=[
                PlannedStep(description="Compute 2*21", tool="calculator", requires_approval=False)
            ],
        ),
        goal_done("Summary with 42"),
    )
    llm.add(ToolArguments, args('{"expression": "2*21"}'))

    task = ctx.tasks.create_task("Summarise and compute")
    assert await ctx.agent.run(task.id) is TaskStatus.COMPLETED
    final = ctx.tasks.get_task(task.id)
    assert len(final.plan.steps) == 2 and final.plan.replans == 1
    assert final.plan.steps[1].id == "step_2"
    assert "REPLANNED" in [e.event for e in ctx.tasks.list_events(task.id)]


async def test_tool_failure_retry_then_continue(ctx: AppContext, llm: ScriptedLLM) -> None:
    llm.add(PlannerOutput, plan(("Divide", "calculator", False)))
    llm.add(ToolArguments, args('{"expression": "1/0"}'), args('{"expression": "1/1"}'))
    llm.add(StepEvaluation, step_ok("retry"), step_ok())
    llm.add(GoalEvaluation, goal_done("1"))

    task = ctx.tasks.create_task("Divide numbers")
    assert await ctx.agent.run(task.id) is TaskStatus.COMPLETED
    calls = ctx.tasks.list_tool_calls(task.id)
    assert [c.status for c in calls] == ["ERROR", "SUCCESS"]
    assert "Math error" in (calls[0].error or "")
    assert ctx.tasks.get_task(task.id).plan.steps[0].attempts == 2


async def test_invalid_tool_arguments_are_corrected(ctx: AppContext, llm: ScriptedLLM) -> None:
    llm.add(PlannerOutput, plan(("Compute", "calculator", False)))
    llm.add(ToolArguments, args('{"wrong": 1}'), args("not json"), args('{"expression": "3*3"}'))
    llm.add(StepEvaluation, step_ok())
    llm.add(GoalEvaluation, goal_done("9"))

    task = ctx.tasks.create_task("Compute 3*3")
    assert await ctx.agent.run(task.id) is TaskStatus.COMPLETED
    assert llm.count(ToolArguments) == 3


async def test_evaluator_can_fail_task(ctx: AppContext, llm: ScriptedLLM) -> None:
    llm.add(PlannerOutput, plan(("Compute", "calculator", False), ("More", "calculator", False)))
    llm.add(ToolArguments, args('{"expression": "1+1"}'))
    llm.add(
        StepEvaluation,
        StepEvaluation(
            success=False,
            summary="x",
            observations=[],
            next_action="fail",
            reason="impossible goal",
        ),
    )
    task = ctx.tasks.create_task("Impossible")
    assert await ctx.agent.run(task.id) is TaskStatus.FAILED
    assert "impossible goal" in ctx.tasks.get_task(task.id).error


async def test_max_iterations_limit(ctx: AppContext, llm: ScriptedLLM) -> None:
    ctx.settings.max_agent_iterations = 3
    llm.add(PlannerOutput, plan(*[(f"step {i}", "calculator", False) for i in range(5)]))
    llm.add(ToolArguments, args('{"expression": "1+1"}'))
    llm.add(StepEvaluation, step_ok())
    task = ctx.tasks.create_task("Loop a lot")
    assert await ctx.agent.run(task.id) is TaskStatus.FAILED
    final = ctx.tasks.get_task(task.id)
    assert "MAX_AGENT_ITERATIONS" in final.error
    assert final.iterations == 3


async def test_task_timeout(ctx: AppContext, llm: ScriptedLLM) -> None:
    ctx.settings.task_timeout_seconds = 0.2  # type: ignore[assignment]

    class SlowLLM(ScriptedLLM):
        async def generate(self, *, system, prompt, schema):  # type: ignore[no-untyped-def]
            await asyncio.sleep(5)

    ctx.agent.planner.llm = SlowLLM()
    task = ctx.tasks.create_task("Slow")
    assert await ctx.agent.run(task.id) is TaskStatus.FAILED
    assert "timed out" in ctx.tasks.get_task(task.id).error


async def test_llm_error_fails_safely(ctx: AppContext, llm: ScriptedLLM) -> None:
    llm.add(PlannerOutput, LLMError("model unavailable"))
    task = ctx.tasks.create_task("Anything")
    assert await ctx.agent.run(task.id) is TaskStatus.FAILED
    assert "model unavailable" in ctx.tasks.get_task(task.id).error


async def test_missing_llm_configuration(settings) -> None:  # type: ignore[no-untyped-def]
    context = build_context(settings, llm=UnconfiguredLLM())
    task = context.tasks.create_task("Anything")
    assert await context.agent.run(task.id) is TaskStatus.FAILED
    assert "OPENAI_API_KEY" in context.tasks.get_task(task.id).error


async def test_paused_task_is_not_advanced(ctx: AppContext, llm: ScriptedLLM) -> None:
    task = ctx.tasks.create_task("Paused")
    ctx.tasks.transition(task.id, TaskStatus.PAUSED)
    assert await ctx.agent.run(task.id) is TaskStatus.PAUSED
    assert llm.calls == []


# --------------------------------------------------------- approvals --


def _webhook_ctx(ctx: AppContext, received: list[dict]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        received.append(json.loads(request.content))
        return httpx.Response(200)

    ctx.tools.get("notification").webhook = WebhookSender(  # type: ignore[attr-defined]
        "https://hooks.example.test/notify", transport=httpx.MockTransport(handler)
    )


def _script_webhook(llm: ScriptedLLM) -> None:
    llm.add(PlannerOutput, plan(("Send the summary to the team webhook", "notification", True)))
    llm.add(
        ToolArguments,
        args('{"title": "Summary", "message": "All good", "channel": "webhook"}'),
    )
    llm.add(StepEvaluation, step_ok())
    llm.add(GoalEvaluation, goal_done("Sent"))


async def test_approval_flow(ctx: AppContext, llm: ScriptedLLM) -> None:
    received: list[dict] = []
    _webhook_ctx(ctx, received)
    _script_webhook(llm)
    task = ctx.tasks.create_task("Notify the team")

    # 1) The agent stops and waits for a human.
    assert await ctx.agent.run(task.id) is TaskStatus.WAITING_APPROVAL
    pending = ctx.approvals.list(status=ApprovalStatus.PENDING, task_id=task.id)
    assert len(pending) == 1 and pending[0].tool == "notification"
    assert pending[0].input["channel"] == "webhook"
    assert received == []  # nothing was sent yet
    assert ctx.tasks.get_task(task.id).plan.steps[0].status is StepStatus.WAITING_APPROVAL

    # 2) Running again without a decision keeps waiting and sends nothing.
    assert await ctx.agent.run(task.id) is TaskStatus.WAITING_APPROVAL

    # 3) Approve -> task re-queued -> completes, sending exactly the approved payload.
    ctx.approvals.decide(pending[0].id, approved=True)
    ctx.tasks.try_transition(task.id, {TaskStatus.WAITING_APPROVAL}, TaskStatus.PENDING)
    assert await ctx.agent.run(task.id) is TaskStatus.COMPLETED
    assert received == [{"title": "Summary", "message": "All good", "task_id": task.id}]
    assert ctx.approvals.get(pending[0].id).consumed
    assert llm.count(ToolArguments) == 1  # approved input was reused, not regenerated


async def test_approval_rejection_skips_step(ctx: AppContext, llm: ScriptedLLM) -> None:
    received: list[dict] = []
    _webhook_ctx(ctx, received)
    _script_webhook(llm)
    task = ctx.tasks.create_task("Notify the team")
    await ctx.agent.run(task.id)
    approval = ctx.approvals.list(task_id=task.id)[0]

    ctx.approvals.decide(approval.id, approved=False, note="not now")
    ctx.tasks.try_transition(task.id, {TaskStatus.WAITING_APPROVAL}, TaskStatus.PENDING)
    await ctx.agent.run(task.id)

    final = ctx.tasks.get_task(task.id)
    assert received == []
    assert final.plan.steps[0].status is StepStatus.SKIPPED
    assert "not now" in final.plan.steps[0].error
    assert "APPROVAL_REJECTED" in [e.event for e in ctx.tasks.list_events(task.id)]


async def test_approval_rejection_fail_policy(ctx: AppContext, llm: ScriptedLLM) -> None:
    ctx.settings.approval_rejection_policy = "fail"
    _webhook_ctx(ctx, [])
    _script_webhook(llm)
    task = ctx.tasks.create_task("Notify the team")
    await ctx.agent.run(task.id)
    approval = ctx.approvals.list(task_id=task.id)[0]
    ctx.approvals.decide(approval.id, approved=False)
    ctx.tasks.try_transition(task.id, {TaskStatus.WAITING_APPROVAL}, TaskStatus.PENDING)
    assert await ctx.agent.run(task.id) is TaskStatus.FAILED


async def test_configured_always_require_approval(ctx: AppContext, llm: ScriptedLLM) -> None:
    ctx.approvals.always_require = {"calculator"}
    llm.add(PlannerOutput, plan(("Compute", "calculator", False)))
    llm.add(ToolArguments, args('{"expression": "1+1"}'))
    task = ctx.tasks.create_task("Compute")
    assert await ctx.agent.run(task.id) is TaskStatus.WAITING_APPROVAL


@pytest.mark.parametrize("decision", [True, False])
async def test_runner_resumes_after_decision(
    ctx: AppContext, llm: ScriptedLLM, decision: bool
) -> None:
    _webhook_ctx(ctx, [])
    _script_webhook(llm)
    task = ctx.tasks.create_task("Notify")
    ctx.runner.submit(task.id)
    await ctx.runner.wait(task.id, timeout=5)
    assert ctx.tasks.get_task(task.id).status is TaskStatus.WAITING_APPROVAL

    approval = ctx.approvals.list(task_id=task.id)[0]
    ctx.approvals.decide(approval.id, approved=decision)
    assert ctx.tasks.try_transition(task.id, {TaskStatus.WAITING_APPROVAL}, TaskStatus.PENDING)
    ctx.runner.submit(task.id)
    await ctx.runner.wait(task.id, timeout=5)
    assert ctx.tasks.get_task(task.id).status is TaskStatus.COMPLETED
