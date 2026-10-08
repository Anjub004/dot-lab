"""HTTP API tests using FastAPI's TestClient and the scripted LLM."""

from __future__ import annotations

import time
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.agent.evaluator import ChangeAssessment, GoalEvaluation, StepEvaluation
from app.agent.executor import ToolArguments
from app.agent.llm import UnconfiguredLLM
from app.agent.planner import PlannerOutput
from app.config import Settings
from app.main import create_app
from app.tools.notifications import WebhookSender
from tests.conftest import ScriptedLLM, args, goal_done, plan, step_ok


def wait_for_status(
    client: TestClient, task_id: str, statuses: set[str], timeout: float = 10
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/tasks/{task_id}").json()
        if body["status"] in statuses:
            return body
        time.sleep(0.05)
    raise AssertionError(f"task never reached {statuses}; last status {body['status']}")


@pytest.fixture
def client(settings: Settings, llm: ScriptedLLM) -> Iterator[TestClient]:
    with TestClient(create_app(settings, llm=llm)) as test_client:
        yield test_client


def script_calculation(llm: ScriptedLLM) -> None:
    llm.add(PlannerOutput, plan(("Compute", "calculator", False)))
    llm.add(ToolArguments, args('{"expression": "6*7"}'))
    llm.add(StepEvaluation, step_ok())
    llm.add(GoalEvaluation, goal_done("6*7 = 42"))


def test_health_and_dashboard(client: TestClient) -> None:
    health = client.get("/api/health").json()
    assert health["status"] == "ok" and health["llm_configured"] is True
    assert {t["name"] for t in health["tools"]} >= {"calculator", "browser"}
    page = client.get("/")
    assert page.status_code == 200 and "Dot Lab" in page.text


def test_api_task_creation(client: TestClient, llm: ScriptedLLM) -> None:
    script_calculation(llm)
    response = client.post("/api/tasks", json={"goal": "  What is 6 times 7?  "})
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"task_id", "goal", "status", "created_at"}
    assert body["goal"] == "What is 6 times 7?"
    assert body["status"] == "PENDING"

    detail = wait_for_status(client, body["task_id"], {"COMPLETED", "FAILED"})
    assert detail["status"] == "COMPLETED", detail["error"]
    assert detail["result"] == "6*7 = 42"
    assert detail["plan"]["steps"][0]["tool"] == "calculator"
    assert detail["tool_calls"][0]["status"] == "SUCCESS"
    assert detail["events"] and detail["memory"]

    listed = client.get("/api/tasks").json()
    assert listed[0]["id"] == body["task_id"] and listed[0]["progress_total"] == 1
    assert client.get("/api/tasks?status=COMPLETED").json()[0]["id"] == body["task_id"]
    overview = client.get("/api/overview").json()
    assert overview["completed_tasks"] == 1 and overview["active_tasks"] == 0


@pytest.mark.parametrize("payload", [{}, {"goal": ""}, {"goal": "   "}, {"goal": "x" * 6000}])
def test_task_input_validation(client: TestClient, payload: dict) -> None:
    assert client.post("/api/tasks", json=payload).status_code == 422


def test_unknown_task_returns_404(client: TestClient) -> None:
    assert client.get("/api/tasks/doesnotexist").status_code == 404
    assert client.post("/api/tasks/doesnotexist/cancel").status_code == 404


def test_task_creation_without_openai_config(settings: Settings) -> None:
    with TestClient(create_app(settings, llm=UnconfiguredLLM())) as c:
        response = c.post("/api/tasks", json={"goal": "hello world"})
        assert response.status_code == 503
        assert "OPENAI_API_KEY" in response.json()["detail"]
        assert c.get("/api/health").json()["llm_configured"] is False


def test_approval_flow_via_api(client: TestClient, llm: ScriptedLLM) -> None:
    sent: list[bytes] = []
    ctx = client.app.state.ctx
    ctx.tools.get("notification").webhook = WebhookSender(
        "https://hooks.example.test/x",
        transport=httpx.MockTransport(lambda r: sent.append(r.content) or httpx.Response(200)),
    )
    llm.add(PlannerOutput, plan(("Post to webhook", "notification", True)))
    llm.add(ToolArguments, args('{"title": "T", "message": "M", "channel": "webhook"}'))
    llm.add(StepEvaluation, step_ok())
    llm.add(GoalEvaluation, goal_done("posted"))

    task_id = client.post("/api/tasks", json={"goal": "Post an update"}).json()["task_id"]
    wait_for_status(client, task_id, {"WAITING_APPROVAL"})
    pending = client.get("/api/approvals").json()
    assert len(pending) == 1 and pending[0]["task_id"] == task_id
    assert client.get("/api/overview").json()["waiting_approvals"] == 1
    assert sent == []

    approval_id = pending[0]["id"]
    response = client.post(f"/api/approvals/{approval_id}/approve", json={"note": "ok"})
    assert response.status_code == 200 and response.json()["status"] == "APPROVED"
    assert client.post(f"/api/approvals/{approval_id}/approve").status_code == 409

    detail = wait_for_status(client, task_id, {"COMPLETED", "FAILED"})
    assert detail["status"] == "COMPLETED"
    assert len(sent) == 1
    assert client.post("/api/approvals/unknown/reject").status_code == 404


def test_pause_resume_cancel(client: TestClient, settings: Settings) -> None:
    ctx = client.app.state.ctx
    task = ctx.tasks.create_task("manual")  # not submitted, stays PENDING
    assert client.post(f"/api/tasks/{task.id}/pause").json()["status"] == "PAUSED"
    assert client.post(f"/api/tasks/{task.id}/pause").status_code == 409
    assert client.post(f"/api/tasks/{task.id}/cancel").json()["status"] == "CANCELLED"
    assert client.post(f"/api/tasks/{task.id}/resume").status_code == 409


def test_monitor_lifecycle(client: TestClient, llm: ScriptedLLM) -> None:
    llm.add(PlannerOutput, plan(("Summarise news", None, False)))
    from app.agent.executor import ReasoningOutput

    llm.add(ReasoningOutput, ReasoningOutput(output="news"))
    llm.add(StepEvaluation, step_ok())
    llm.add(GoalEvaluation, goal_done("Version 1"), goal_done("Version 2"))
    llm.add(ChangeAssessment, ChangeAssessment(meaningful_change=True, summary="New release"))

    assert (
        client.post(
            "/api/monitors", json={"name": "M", "goal": "watch", "interval_minutes": 1}
        ).status_code
        == 422
    )
    monitor = client.post(
        "/api/monitors",
        json={"name": "AI News Monitor", "goal": "Monitor AI agents", "interval_minutes": 360},
    ).json()

    # Run 1 -> baseline, no notification.
    first = client.post(f"/api/monitors/{monitor['id']}/run").json()["task_id"]
    wait_for_status(client, first, {"COMPLETED"})
    time.sleep(0.2)
    assert client.get("/api/notifications").json() == []

    # Run 2 -> meaningful change -> notification.
    second = client.post(f"/api/monitors/{monitor['id']}/run").json()["task_id"]
    wait_for_status(client, second, {"COMPLETED"})
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not client.get("/api/notifications").json():
        time.sleep(0.05)
    notes = client.get("/api/notifications").json()
    assert notes[0]["title"] == "Monitor update: AI News Monitor"
    assert notes[0]["message"] == "New release"
    listed = client.get("/api/monitors").json()[0]
    assert listed["last_result"] == "Version 2"

    assert client.post(f"/api/monitors/{monitor['id']}/disable").json()["enabled"] is False
    assert client.delete(f"/api/monitors/{monitor['id']}").status_code == 204
    assert client.post(f"/api/monitors/{monitor['id']}/run").status_code == 404


def test_api_key_protection(settings: Settings, llm: ScriptedLLM) -> None:
    settings.dot_lab_api_key = SecretStr("s3cret-key")
    with TestClient(create_app(settings, llm=llm)) as c:
        assert c.get("/api/health").status_code == 200  # health stays public
        assert c.get("/api/tasks").status_code == 401
        assert c.get("/api/tasks", headers={"X-API-Key": "wrong"}).status_code == 401
        assert c.get("/api/tasks", headers={"X-API-Key": "s3cret-key"}).status_code == 200


def test_tools_and_memory_endpoints(client: TestClient) -> None:
    tools = {t["name"]: t for t in client.get("/api/tools").json()}
    assert tools["web_search"]["available"] is False
    from app.memory.models import MemoryCreate, MemoryKind

    record = client.app.state.ctx.memory.save(
        MemoryCreate(kind=MemoryKind.NOTE, content="agents remember")
    )
    assert client.get("/api/memory/search", params={"q": "agents"}).json()[0]["id"] == record.id
    assert client.delete(f"/api/memory/{record.id}").status_code == 204
    assert client.delete(f"/api/memory/{record.id}").status_code == 404
