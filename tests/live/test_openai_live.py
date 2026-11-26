"""Live tests against the real OpenAI API.

These are skipped in the normal test run and in CI. Run them on your own
computer, with your own key, when you want to check the whole agent end to end:

    pytest -m live -v

They read OPENAI_API_KEY and OPENAI_MODEL from the environment or from .env.
Each run makes roughly 10-20 model calls (a few cents with small models). Model
answers vary, so the assertions only check facts that do not depend on wording.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from dotenv import dotenv_values

from app.agent.llm import OpenAILLM
from app.agent.state import TaskStatus
from app.config import PROJECT_ROOT, Settings
from app.context import AppContext, build_context
from tests.conftest import LIVE_CREDENTIALS

pytestmark = pytest.mark.live

_file = dotenv_values(PROJECT_ROOT / ".env")
API_KEY = LIVE_CREDENTIALS["OPENAI_API_KEY"] or (_file.get("OPENAI_API_KEY") or "")
MODEL = LIVE_CREDENTIALS["OPENAI_MODEL"] or (_file.get("OPENAI_MODEL") or "")

if not (API_KEY and MODEL):
    pytest.skip(
        "OPENAI_API_KEY and OPENAI_MODEL are needed for live tests", allow_module_level=True
    )

DEMO = PROJECT_ROOT / "examples" / "data" / "demo_sales.csv"


@pytest.fixture
def live_ctx(tmp_path: Path) -> AppContext:
    settings = Settings(
        openai_api_key=API_KEY,
        openai_model=MODEL,
        database_url=f"sqlite:///{tmp_path / 'live.db'}",
        workspace_dir=tmp_path / "workspace",
        enable_monitor_scheduler=False,
        tool_permission_level="read",  # no file writes or internet during live tests
        max_task_tokens=150_000,
        task_timeout_seconds=300,
    )
    ctx = build_context(settings)
    (settings.workspace_dir / "demo").mkdir(parents=True, exist_ok=True)
    shutil.copy(DEMO, settings.workspace_dir / "demo" / "demo_sales.csv")
    yield ctx
    ctx.db.dispose()


async def test_configured_model_is_available() -> None:
    check = await OpenAILLM(API_KEY, MODEL).check_model()
    assert check.status == "available", check.detail


async def test_calculator_task_end_to_end(live_ctx: AppContext) -> None:
    task = live_ctx.tasks.create_task(
        "Use the calculator tool to compute 1234 * 5678 and give me the result."
    )
    status = await live_ctx.agent.run(task.id)
    final = live_ctx.tasks.get_task(task.id)
    assert status is TaskStatus.COMPLETED, final.error
    assert "7006652" in (final.result or "").replace(",", "").replace(" ", "")
    tools = [c.tool for c in live_ctx.tasks.list_tool_calls(task.id)]
    assert "calculator" in tools
    usage = live_ctx.usage.totals(task.id)
    assert usage.llm_calls >= 3 and usage.total_tokens > 0  # real usage was recorded


async def test_csv_analysis_task_end_to_end(live_ctx: AppContext) -> None:
    task = live_ctx.tasks.create_task(
        "Analyse the file demo/demo_sales.csv: calculate total revenue per branch "
        "and tell me which branch has the highest total revenue."
    )
    status = await live_ctx.agent.run(task.id)
    final = live_ctx.tasks.get_task(task.id)
    assert status is TaskStatus.COMPLETED, final.error
    assert "Northgate" in (final.result or "")  # 949,805.30, computed from the demo data
    calls = live_ctx.tasks.list_tool_calls(task.id)
    assert any(c.tool in {"csv_analyzer", "data_summary"} and c.status == "SUCCESS" for c in calls)
