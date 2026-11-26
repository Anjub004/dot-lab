"""Clarification questions (WAITING_INPUT) and downloadable task reports."""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.agent.evaluator import GoalEvaluation, StepEvaluation
from app.agent.executor import ToolArguments
from app.agent.planner import ClarificationRequest, Planner, PlannerOutput
from app.agent.state import MAX_CLARIFICATIONS, TaskStatus, can_transition
from app.agent.usage import Usage, build_report
from app.config import Settings
from app.context import AppContext
from app.main import create_app
from app.services.report_service import build_task_report
from app.services.task_service import InvalidTransitionError, ToolCall
from app.tools.calculator import CalculatorTool
from tests.conftest import ScriptedLLM, args, goal_done, plan, question, step_ok


def script_after_answer(llm: ScriptedLLM) -> None:
    llm.add(PlannerOutput, plan(("Compute", "calculator", False)))
    llm.add(ToolArguments, args('{"expression": "6*7"}'))
    llm.add(StepEvaluation, step_ok())
    llm.add(GoalEvaluation, goal_done("June revenue: 42"))


# ------------------------------------------------------------- state rules --


def test_waiting_input_transitions() -> None:
    assert can_transition(TaskStatus.PLANNING, TaskStatus.WAITING_INPUT)
    assert can_transition(TaskStatus.WAITING_INPUT, TaskStatus.PENDING)
    assert can_transition(TaskStatus.WAITING_INPUT, TaskStatus.CANCELLED)
    assert not can_transition(TaskStatus.RUNNING, TaskStatus.WAITING_INPUT)
    assert not can_transition(TaskStatus.WAITING_INPUT, TaskStatus.RUNNING)


async def test_planner_question_rules() -> None:
    tools = [CalculatorTool()]
    llm = ScriptedLLM({PlannerOutput: [question("Which month?")]})
    result = await Planner(llm, max_steps=5).create_plan("Report revenue", tools)
    assert result == ClarificationRequest(question="Which month?")

    # When asking is not allowed, a question is rejected with feedback and re-asked.
    llm = ScriptedLLM({PlannerOutput: [question("Which month?"), plan(("Do it", None, False))]})
    planned = await Planner(llm, max_steps=5).create_plan(
        "Report revenue", tools, allow_clarification=False
    )
    assert not isinstance(planned, ClarificationRequest) and len(planned.steps) == 1
    assert "Asking is NOT allowed now" in llm.systems[0][1]
    allowed = ScriptedLLM({PlannerOutput: [question("Which month?")]})
    await Planner(allowed, max_steps=5).create_plan("Report revenue", tools)
    assert "You may ask at most one question now" in allowed.systems[0][1]
    assert "Questions are not allowed now" in llm.calls[1][1]


# ------------------------------------------------------------- agent flow --


async def test_agent_asks_then_continues_with_the_answer(ctx: AppContext, llm: ScriptedLLM) -> None:
    llm.add(PlannerOutput, question("Which month should I report on?"))
    task = ctx.tasks.create_task("Report revenue for the branches")

    assert await ctx.agent.run(task.id) is TaskStatus.WAITING_INPUT
    waiting = ctx.tasks.get_task(task.id)
    assert waiting.plan is None
    assert [c.question for c in waiting.clarifications] == ["Which month should I report on?"]
    assert "CLARIFICATION_REQUESTED" in [e.event for e in ctx.tasks.list_events(task.id)]
    assert (
        await ctx.agent.run(task.id) is TaskStatus.WAITING_INPUT
    )  # nothing happens until answered

    llm.script.clear()
    llm.calls.clear()
    script_after_answer(llm)
    ctx.tasks.answer_clarification(task.id, "June 2026")
    assert ctx.tasks.get_task(task.id).status is TaskStatus.PENDING
    assert await ctx.agent.run(task.id) is TaskStatus.COMPLETED

    planner_prompt = next(p for s, p in llm.calls if s is PlannerOutput)
    assert "Clarifications from the user" in planner_prompt
    assert "A: June 2026" in planner_prompt
    args_prompt = next(p for s, p in llm.calls if s is ToolArguments)
    assert "A: June 2026" in args_prompt  # every module sees the clarified goal
    final = ctx.tasks.get_task(task.id)
    assert final.clarifications[0].answer == "June 2026" and final.clarifications[0].answered_at


async def test_question_limit_forces_a_plan(ctx: AppContext, llm: ScriptedLLM) -> None:
    task = ctx.tasks.create_task("Vague goal")
    for i in range(MAX_CLARIFICATIONS):
        llm.script.clear()
        llm.add(PlannerOutput, question(f"Question {i}?"))
        assert await ctx.agent.run(task.id) is TaskStatus.WAITING_INPUT
        ctx.tasks.answer_clarification(task.id, f"Answer {i}")

    llm.script.clear()
    llm.calls.clear()
    # The model tries to ask a third time; the planner refuses and it plans instead.
    llm.add(
        PlannerOutput, question("Yet another question?"), plan(("Compute", "calculator", False))
    )
    llm.add(ToolArguments, args('{"expression": "1+1"}'))
    llm.add(StepEvaluation, step_ok())
    llm.add(GoalEvaluation, goal_done("2"))
    assert await ctx.agent.run(task.id) is TaskStatus.COMPLETED
    assert len(ctx.tasks.get_task(task.id).clarifications) == MAX_CLARIFICATIONS


def test_answer_requires_an_open_question(ctx: AppContext) -> None:
    task = ctx.tasks.create_task("Anything")
    with pytest.raises(InvalidTransitionError, match="not WAITING_INPUT"):
        ctx.tasks.answer_clarification(task.id, "x")


# ------------------------------------------------------------------- API --


@pytest.fixture
def client(settings: Settings, llm: ScriptedLLM) -> Iterator[TestClient]:
    with TestClient(create_app(settings, llm=llm)) as test_client:
        yield test_client


def wait_status(client: TestClient, task_id: str, wanted: str) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        body = client.get(f"/api/tasks/{task_id}").json()
        if body["status"] == wanted:
            return body
        time.sleep(0.05)
    raise AssertionError(f"task never reached {wanted}; last {body['status']}")


def test_answer_via_api(client: TestClient, llm: ScriptedLLM) -> None:
    llm.add(PlannerOutput, question("Which month?"), plan(("Compute", "calculator", False)))
    llm.add(ToolArguments, args('{"expression": "6*7"}'))
    llm.add(StepEvaluation, step_ok())
    llm.add(GoalEvaluation, goal_done("June revenue: 42"))

    task_id = client.post("/api/tasks", json={"goal": "Report revenue"}).json()["task_id"]
    body = wait_status(client, task_id, "WAITING_INPUT")
    assert body["clarifications"][0]["question"] == "Which month?"
    assert client.get("/api/overview").json()["waiting_input"] == 1

    assert client.post(f"/api/tasks/{task_id}/answer", json={"answer": "  "}).status_code == 422
    assert client.post(f"/api/tasks/{task_id}/answer", json={"answer": "June"}).status_code == 200
    done = wait_status(client, task_id, "COMPLETED")
    assert done["clarifications"][0]["answer"] == "June"
    assert client.post(f"/api/tasks/{task_id}/answer", json={"answer": "x"}).status_code == 409


def test_waiting_task_can_be_cancelled(client: TestClient, llm: ScriptedLLM) -> None:
    llm.add(PlannerOutput, question("Which month?"))
    task_id = client.post("/api/tasks", json={"goal": "Report revenue"}).json()["task_id"]
    wait_status(client, task_id, "WAITING_INPUT")
    assert client.post(f"/api/tasks/{task_id}/cancel").json()["status"] == "CANCELLED"


def test_conversation_message_answers_the_waiting_task(
    client: TestClient, llm: ScriptedLLM
) -> None:
    llm.add(PlannerOutput, question("Which month?"), plan(("Compute", "calculator", False)))
    llm.add(ToolArguments, args('{"expression": "6*7"}'))
    llm.add(StepEvaluation, step_ok())
    llm.add(GoalEvaluation, goal_done("June revenue: 42"))

    conv = client.post("/api/conversations", json={}).json()["id"]
    first = client.post(f"/api/conversations/{conv}/messages", json={"content": "Report revenue"})
    task_id = first.json()["task_id"]
    wait_status(client, task_id, "WAITING_INPUT")
    messages = client.get(f"/api/conversations/{conv}").json()["messages"]
    assert [m["content"] for m in messages] == ["Report revenue", "Which month?"]

    answer = client.post(f"/api/conversations/{conv}/messages", json={"content": "June please"})
    assert answer.json()["task_id"] == task_id  # same task, no new one
    wait_status(client, task_id, "COMPLETED")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        messages = client.get(f"/api/conversations/{conv}").json()["messages"]
        if len(messages) == 4:
            break
        time.sleep(0.05)
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[-1]["content"] == "June revenue: 42"
    assert len(client.get("/api/tasks").json()) == 1


def test_report_endpoint(client: TestClient, llm: ScriptedLLM) -> None:
    llm.usage_per_call = (100, 10)
    llm.add(PlannerOutput, plan(("Compute 6*7", "calculator", False)))
    llm.add(ToolArguments, args('{"expression": "6*7"}'))
    llm.add(StepEvaluation, step_ok())
    llm.add(GoalEvaluation, goal_done("6*7 = 42"))
    task_id = client.post("/api/tasks", json={"goal": "What is 6 times 7?"}).json()["task_id"]
    wait_status(client, task_id, "COMPLETED")

    response = client.get(f"/api/tasks/{task_id}/report")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.headers["content-disposition"] == (
        f'attachment; filename="dot-lab-task-{task_id[:8]}.md"'
    )
    text = response.text
    assert text.startswith("# Dot Lab task report")
    assert "| Status | **COMPLETED** |" in text
    assert "What is 6 times 7?" in text and "6*7 = 42" in text
    assert "| step_1 | Compute 6*7 | calculator | COMPLETED | 1 |" in text
    assert "`calculator` · SUCCESS" in text
    assert "| Tokens | 440 (400 in / 40 out) |" in text
    assert "**TASK_COMPLETED**" in text

    inline = client.get(f"/api/tasks/{task_id}/report", params={"download": "false"})
    assert inline.headers["content-disposition"].startswith("inline;")
    assert client.get("/api/tasks/missing/report").status_code == 404


def test_report_escapes_tables_and_truncates_output(ctx: AppContext) -> None:
    task = ctx.tasks.create_task("Goal with | pipe")
    view = ctx.tasks.get_task(task.id)
    call = ToolCall(
        id=1,
        task_id=task.id,
        step_id="step_1",
        tool="browser",
        input={},
        output="x" * 5000 + "```",
        status="SUCCESS",
        error=None,
        attempt=1,
        duration_ms=5,
        created_at=datetime.now(UTC),
    )
    text = build_task_report(
        view,
        events=[],
        tool_calls=[call],
        approvals=[],
        usage=build_report(Usage(), None, None, None),
        generated_at=datetime(2026, 11, 26, tzinfo=UTC),
    )
    assert "4,203 more characters not shown" in text
    assert text.count("```") == 2  # tool output cannot break out of its code block
    assert "_No plan was created._" in text and "_No approvals were needed._" in text
