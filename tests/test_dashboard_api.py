"""API endpoints used by the dashboard: activity, settings, saved reports, task search."""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.agent.evaluator import GoalEvaluation, StepEvaluation
from app.agent.executor import ToolArguments
from app.agent.planner import PlannerOutput
from app.config import Settings
from app.main import create_app
from tests.conftest import ScriptedLLM, args, goal_done, plan, step_ok


@pytest.fixture
def client(settings: Settings, llm: ScriptedLLM) -> Iterator[TestClient]:
    with TestClient(create_app(settings, llm=llm)) as test_client:
        yield test_client


def run_calc(client: TestClient, llm: ScriptedLLM, goal: str) -> str:
    llm.script.clear()
    llm.add(PlannerOutput, plan(("Compute", "calculator", False)))
    llm.add(ToolArguments, args('{"expression": "6*7"}'))
    llm.add(StepEvaluation, step_ok())
    llm.add(GoalEvaluation, goal_done("42"))
    task_id = client.post("/api/tasks", json={"goal": goal}).json()["task_id"]
    deadline = time.monotonic() + 10
    while client.get(f"/api/tasks/{task_id}").json()["status"] != "COMPLETED":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return task_id


def test_activity_feed(client: TestClient, llm: ScriptedLLM) -> None:
    task_id = run_calc(client, llm, "Compute 6 times 7")
    feed = client.get("/api/activity").json()
    assert feed[0]["event"] == "TASK_COMPLETED"
    assert feed[0]["task_id"] == task_id and feed[0]["task_goal"] == "Compute 6 times 7"
    ids = [e["id"] for e in feed]
    assert ids == sorted(ids, reverse=True)

    older = client.get("/api/activity", params={"before_id": ids[1], "limit": 2}).json()
    assert [e["id"] for e in older] == ids[2:4]
    tools = client.get("/api/activity", params={"kind": "tools"}).json()
    assert {e["event"] for e in tools} == {"TOOL_CALLED"}
    tasks = client.get("/api/activity", params={"kind": "tasks"}).json()
    assert {e["event"] for e in tasks} == {"TASK_CREATED", "TASK_COMPLETED"}
    assert client.get("/api/activity", params={"kind": "errors"}).json() == []
    assert client.get("/api/activity", params={"kind": "bogus"}).status_code == 422


def test_task_search_and_finished_filter(client: TestClient, llm: ScriptedLLM) -> None:
    run_calc(client, llm, "Northgate revenue_100% check")
    run_calc(client, llm, "Supplier fill rate")
    found = client.get("/api/tasks", params={"q": "northgate"}).json()
    assert [t["goal"] for t in found] == ["Northgate revenue_100% check"]
    # Wildcards are literal: '_' and '%' only match themselves.
    assert len(client.get("/api/tasks", params={"q": "revenue_100%"}).json()) == 1
    assert client.get("/api/tasks", params={"q": "revenueX100"}).json() == []
    assert len(client.get("/api/tasks", params={"finished": True}).json()) == 2
    page = client.get("/api/tasks", params={"limit": 1, "offset": 1}).json()
    assert [t["goal"] for t in page] == ["Northgate revenue_100% check"]


def test_overview_totals(client: TestClient, llm: ScriptedLLM) -> None:
    llm.usage_per_call = (10, 5)
    run_calc(client, llm, "Compute")
    overview = client.get("/api/overview").json()
    assert overview["llm_calls_total"] == 4 and overview["tokens_used_total"] == 60


def test_settings_view_has_no_secrets(settings: Settings, llm: ScriptedLLM) -> None:
    from pydantic import SecretStr

    secret = "sk-super-secret-value"
    configured = settings.model_copy(
        update={
            "openai_api_key": SecretStr(secret),
            "openai_model": "my-model",
            "dot_lab_api_key": SecretStr("local-key-123"),
            "tool_permission_level": "read",
        }
    )
    with TestClient(create_app(configured, llm=llm)) as c:
        response = c.get("/api/settings", headers={"X-API-Key": "local-key-123"})
    body = response.json()
    assert body["openai_model"] == "my-model" and body["api_key_protection"] is True
    assert body["tool_permission_level"] == "read"
    assert body["limits"]["MAX_AGENT_ITERATIONS"] == settings.max_agent_iterations
    assert secret not in response.text and "local-key-123" not in response.text


def test_saved_reports(client: TestClient, settings: Settings) -> None:
    folder = settings.workspace_dir / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "june_sales.md").write_text("# June\n", encoding="utf-8")
    time.sleep(0.01)
    (folder / "july_sales.md").write_text("# July\n", encoding="utf-8")
    (folder / "notes.txt").write_text("not a report")
    (folder / ".hidden.md").write_text("hidden")

    listed = client.get("/api/reports").json()
    assert [r["name"] for r in listed] == ["july_sales.md", "june_sales.md"]
    report = client.get("/api/reports/june_sales.md")
    assert report.text == "# June\n"
    assert report.headers["content-disposition"] == 'attachment; filename="june_sales.md"'
    inline = client.get("/api/reports/june_sales.md", params={"download": False})
    assert inline.headers["content-disposition"].startswith("inline")
    for bad in ["notes.txt", ".hidden.md", "missing.md", "..%2Fsecret.md"]:
        assert client.get(f"/api/reports/{bad}").status_code == 404


def test_reports_empty_when_folder_missing(client: TestClient) -> None:
    assert client.get("/api/reports").json() == []
