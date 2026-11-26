"""Short-term conversation context and the memory / conversation APIs."""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.agent.evaluator import GoalEvaluation, StepEvaluation
from app.agent.executor import ToolArguments
from app.agent.planner import PlannerOutput
from app.agent.state import TaskStatus
from app.config import Settings
from app.context import AppContext
from app.main import create_app
from app.memory.models import MemoryCreate, MemoryKind
from app.services.conversation_service import DEFAULT_TITLE, ConversationService
from tests.conftest import ScriptedLLM, args, goal_done, plan, step_ok


def script_calc(llm: ScriptedLLM, result: str = "6*7 = 42") -> None:
    llm.add(PlannerOutput, plan(("Compute", "calculator", False)))
    llm.add(ToolArguments, args('{"expression": "6*7"}'))
    llm.add(StepEvaluation, step_ok())
    llm.add(GoalEvaluation, goal_done(result))


# ------------------------------------------------------------- service --


def test_title_comes_from_first_message(ctx: AppContext) -> None:
    conv = ctx.conversations.create()
    assert conv.title == DEFAULT_TITLE
    ctx.conversations.add_message(conv.id, "user", "  Rank   branches by gross profit  ")
    ctx.conversations.add_message(conv.id, "user", "Second message does not rename")
    assert ctx.conversations.get(conv.id).title == "Rank branches by gross profit"
    long = ctx.conversations.create()
    ctx.conversations.add_message(long.id, "user", "word " * 40)
    assert len(ctx.conversations.get(long.id).title) == 60


def test_context_excludes_the_tasks_own_messages(ctx: AppContext) -> None:
    conv = ctx.conversations.create("Sales")
    t1 = ctx.tasks.create_task("May revenue by branch", conversation_id=conv.id)
    ctx.conversations.add_message(conv.id, "user", "May revenue by branch", t1.id)
    ctx.conversations.add_message(conv.id, "assistant", "Northgate led May.", t1.id)
    t2 = ctx.tasks.create_task("Same for June", conversation_id=conv.id)
    ctx.conversations.add_message(conv.id, "user", "Same for June", t2.id)

    context = ctx.conversations.context_for_task(conv.id, t2.id, max_messages=10)
    assert context == "USER: May revenue by branch\n\nASSISTANT: Northgate led May."
    assert ctx.conversations.context_for_task(conv.id, t2.id, max_messages=0) == ""


def test_context_keeps_newest_messages_within_limits(ctx: AppContext) -> None:
    service: ConversationService = ctx.conversations
    conv = service.create()
    for i in range(12):
        service.add_message(conv.id, "user", f"message {i} " + "x" * 1500)
    context = service.context_for_task(conv.id, "no-task", max_messages=10)
    assert "message 11" in context and "message 0 " not in context
    assert len(context) <= 8000 + 10 * 4  # character budget plus separators
    service.add_message(conv.id, "assistant", "y" * 5000)
    newest = service.context_for_task(conv.id, "no-task", max_messages=1)
    assert newest.endswith("[...]") and len(newest) < 2100  # one long reply is trimmed


# ---------------------------------------------------------------- agent --


async def test_follow_up_task_sees_conversation_and_replies(
    ctx: AppContext, llm: ScriptedLLM
) -> None:
    conv = ctx.conversations.create()
    first = ctx.tasks.create_task("Compute 6*7", conversation_id=conv.id)
    ctx.conversations.add_message(conv.id, "user", "Compute 6*7", first.id)
    script_calc(llm)
    assert await ctx.agent.run(first.id) is TaskStatus.COMPLETED

    second = ctx.tasks.create_task("Now double it", conversation_id=conv.id)
    ctx.conversations.add_message(conv.id, "user", "Now double it", second.id)
    llm.calls.clear()
    assert await ctx.agent.run(second.id) is TaskStatus.COMPLETED

    planner_prompt = next(p for s, p in llm.calls if s is PlannerOutput)
    assert "Recent conversation" in planner_prompt
    assert "USER: Compute 6*7" in planner_prompt and "ASSISTANT: 6*7 = 42" in planner_prompt
    assert "Now double it" not in planner_prompt.split("Goal:")[0]  # own message excluded
    review_prompt = next(p for s, p in llm.calls if s is GoalEvaluation)
    assert "ASSISTANT: 6*7 = 42" in review_prompt

    roles = [(m.role, m.task_id) for m in ctx.conversations.messages(conv.id)]
    assert roles == [
        ("user", first.id),
        ("assistant", first.id),
        ("user", second.id),
        ("assistant", second.id),
    ]


async def test_failed_task_reply_explains_failure(ctx: AppContext, llm: ScriptedLLM) -> None:
    conv = ctx.conversations.create()
    task = ctx.tasks.create_task("Compute and chart", conversation_id=conv.id)
    llm.add(PlannerOutput, plan(("Compute", "calculator", False)))
    llm.add(ToolArguments, args('{"expression": "6*7"}'))
    llm.add(StepEvaluation, step_ok())
    llm.add(
        GoalEvaluation,
        GoalEvaluation(
            goal_satisfied=False, final_result="42", reason="no chart", additional_steps=[]
        ),
    )
    assert await ctx.agent.run(task.id) is TaskStatus.FAILED
    reply = ctx.conversations.messages(conv.id)[-1]
    assert reply.role == "assistant" and reply.content.startswith("The task failed:")
    assert "Partial result" in reply.content


async def test_tasks_without_conversation_get_no_context(ctx: AppContext, llm: ScriptedLLM) -> None:
    script_calc(llm)
    task = ctx.tasks.create_task("Compute 6*7")
    assert await ctx.agent.run(task.id) is TaskStatus.COMPLETED
    assert "Recent conversation" not in llm.calls[0][1]


async def test_user_notes_reach_the_planner(ctx: AppContext, llm: ScriptedLLM) -> None:
    ctx.memory.save(
        MemoryCreate(kind=MemoryKind.NOTE, content="Gross profit excludes VAT", category="finance")
    )
    script_calc(llm)
    task = ctx.tasks.create_task("Explain gross profit for Northgate")
    await ctx.agent.run(task.id)
    assert "Gross profit excludes VAT" in llm.calls[0][1]


# ------------------------------------------------------------------ API --


@pytest.fixture
def client(settings: Settings, llm: ScriptedLLM) -> Iterator[TestClient]:
    with TestClient(create_app(settings, llm=llm)) as test_client:
        yield test_client


def wait_for(client: TestClient, conv_id: str, count: int) -> list[dict]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        messages = client.get(f"/api/conversations/{conv_id}").json()["messages"]
        if len(messages) >= count:
            return messages
        time.sleep(0.05)
    raise AssertionError(f"conversation never reached {count} messages")


def test_conversation_api_flow(client: TestClient, llm: ScriptedLLM) -> None:
    script_calc(llm)
    conv = client.post("/api/conversations", json={}).json()
    posted = client.post(
        f"/api/conversations/{conv['id']}/messages", json={"content": "  What is 6 times 7?  "}
    )
    assert posted.status_code == 201
    body = posted.json()
    assert body["message"]["content"] == "What is 6 times 7?"

    messages = wait_for(client, conv["id"], 2)
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["content"] == "6*7 = 42"
    task = client.get(f"/api/tasks/{body['task_id']}").json()
    assert task["conversation_id"] == conv["id"]

    listed = client.get("/api/conversations").json()
    assert listed[0]["id"] == conv["id"] and listed[0]["message_count"] == 2
    assert listed[0]["title"] == "What is 6 times 7?"

    # Deleting the conversation keeps the task and its history.
    assert client.delete(f"/api/conversations/{conv['id']}").status_code == 204
    assert client.get(f"/api/conversations/{conv['id']}").status_code == 404
    kept = client.get(f"/api/tasks/{body['task_id']}").json()
    assert kept["status"] == "COMPLETED" and kept["conversation_id"] is None


def test_conversation_api_errors(client: TestClient) -> None:
    assert client.get("/api/conversations/missing").status_code == 404
    assert (
        client.post("/api/conversations/missing/messages", json={"content": "hello"}).status_code
        == 404
    )
    conv = client.post("/api/conversations", json={"title": "Test"}).json()
    assert conv["title"] == "Test"
    assert (
        client.post(f"/api/conversations/{conv['id']}/messages", json={"content": "  "}).status_code
        == 422
    )


def test_memory_api(client: TestClient) -> None:
    created = client.post(
        "/api/memory", json={"content": " Fiscal year starts in April ", "category": "Finance"}
    )
    assert created.status_code == 201
    note = created.json()
    assert note["kind"] == "note" and note["category"] == "finance"
    assert note["content"] == "Fiscal year starts in April"
    client.post(
        "/api/memory", json={"content": "Northgate is the flagship", "category": "branches"}
    )

    assert [m["id"] for m in client.get("/api/memory?category=finance").json()] == [note["id"]]
    assert client.get(f"/api/memory/{note['id']}").json()["content"] == note["content"]
    categories = {c["category"]: c["count"] for c in client.get("/api/memory/categories").json()}
    assert categories == {"finance": 1, "branches": 1}
    found = client.get("/api/memory/search", params={"q": "fiscal april"}).json()
    assert found[0]["id"] == note["id"]

    patched = client.patch(f"/api/memory/{note['id']}", json={"category": "accounting"})
    assert patched.json()["category"] == "accounting" and patched.json()["updated_at"]

    assert client.patch(f"/api/memory/{note['id']}", json={}).status_code == 422
    assert (
        client.post("/api/memory", json={"content": "x", "category": "bad/cat"}).status_code == 422
    )
    assert client.get("/api/memory?category=bad/cat").status_code == 422
    assert client.patch("/api/memory/999999", json={"content": "x"}).status_code == 404
    assert client.get("/api/memory/999999").status_code == 404
    assert client.delete(f"/api/memory/{note['id']}").status_code == 204
    assert client.delete(f"/api/memory/{note['id']}").status_code == 404
