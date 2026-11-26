"""Generate the README screenshots (optional, needs Playwright).

Runs Dot Lab in-process with the scripted fake LLM and the bundled demo sales
data. The tools really run (pandas computes the figures, the report is really
written); only the model's wording is scripted, and every number in it matches
what the tools compute from examples/data/demo_sales.csv.

    pip install playwright pillow && playwright install chromium
    python scripts/make_screenshots.py --out docs/screenshots
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from app.agent.evaluator import GoalEvaluation, StepEvaluation  # noqa: E402
from app.agent.executor import ReasoningOutput, ToolArguments  # noqa: E402
from app.agent.llm import ModelCheck  # noqa: E402
from app.agent.planner import PlannedStep, PlannerOutput  # noqa: E402
from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.conftest import ScriptedLLM, goal_done  # noqa: E402

PORT = 8798
BASE = f"http://127.0.0.1:{PORT}"
DEMO = ROOT / "examples" / "data" / "demo_sales.csv"


class DemoLLM(ScriptedLLM):
    """The scripted test LLM, labelled honestly as a demo in Settings."""

    async def check_model(self) -> ModelCheck:
        return ModelCheck(status="available", detail="Demo mode: scripted model responses")


def plan(summary: str, *steps: tuple[str, str | None, bool]) -> PlannerOutput:
    return PlannerOutput(
        summary=summary,
        steps=[PlannedStep(description=d, tool=t, requires_approval=a) for d, t, a in steps],
        clarification_question=None,
    )


def step_ok(summary: str, observations: list[str] | None = None) -> StepEvaluation:
    return StepEvaluation(
        success=True,
        summary=summary,
        observations=observations or [],
        next_action="continue",
        reason="The tool output answers the step.",
    )


def args(json_text: str, explanation: str) -> ToolArguments:
    return ToolArguments(arguments_json=json_text, explanation=explanation)


def wait(client: httpx.Client, task_id: str, statuses: set[str]) -> None:
    deadline = time.monotonic() + 30
    while client.get(f"/api/tasks/{task_id}").json()["status"] not in statuses:
        if time.monotonic() > deadline:
            raise TimeoutError(task_id)
        time.sleep(0.1)


def wait_messages(client: httpx.Client, conv: str, count: int) -> None:
    deadline = time.monotonic() + 30
    while len(client.get(f"/api/conversations/{conv}").json()["messages"]) < count:
        if time.monotonic() > deadline:
            raise TimeoutError(conv)
        time.sleep(0.1)


def seed(client: httpx.Client, llm: DemoLLM) -> str:
    """Create realistic tasks, a conversation, memory and a pending approval."""
    usage = (1850, 240)
    llm.usage_per_call = usage

    # 1. Conversation: revenue ranking, then a follow-up that saves a report.
    llm.script.clear()
    llm.add(
        PlannerOutput,
        plan(
            "Rank branches by total revenue",
            ("Total revenue per branch from demo/demo_sales.csv", "csv_analyzer", False),
        ),
    )
    llm.add(
        ToolArguments,
        args(
            '{"path": "demo/demo_sales.csv", "operation": "aggregate", "group_by": ["branch"],'
            ' "metrics": [{"column": "revenue", "agg": "sum"}], "limit": 8}',
            "Sum revenue per branch to find the highest.",
        ),
    )
    llm.add(
        StepEvaluation,
        step_ok(
            "Revenue totals computed for all 8 branches",
            observations=["Northgate is the top branch by revenue (Jan-Jun 2026)"],
        ),
    )
    llm.add(
        GoalEvaluation,
        goal_done(
            "Northgate had the highest revenue: 949,805.30, ahead of Riverside (838,114.94) "
            "and Lakeside (677,509.72). Airport Road was lowest at 384,857.12."
        ),
    )
    conv = client.post("/api/conversations", json={}).json()["id"]
    client.post(
        f"/api/conversations/{conv}/messages",
        json={"content": "Which branch had the highest revenue in demo/demo_sales.csv?"},
    )
    wait_messages(client, conv, 2)

    llm.script.clear()
    llm.add(
        PlannerOutput,
        plan(
            "Summarise the top 3 branches and save a report",
            ("Write a short summary of the top 3 branches", None, False),
            ("Save it as reports/top_branches.md", "report_generator", False),
        ),
    )
    llm.add(
        ReasoningOutput,
        ReasoningOutput(
            output="Top 3 branches by revenue: Northgate 949,805.30, Riverside 838,114.94, "
            "Lakeside 677,509.72."
        ),
    )
    llm.add(
        ToolArguments,
        args(
            '{"title": "Top branches by revenue (Jan-Jun 2026)", "filename": "top_branches",'
            ' "summary": "Northgate leads, followed by Riverside and Lakeside.",'
            ' "sections": [{"heading": "Ranking", "body": "| Branch | Revenue |\\n|---|---|\\n'
            "| Northgate | 949,805.30 |\\n| Riverside | 838,114.94 |\\n"
            '| Lakeside | 677,509.72 |"}]}',
            "Save the ranking from the previous answer as a Markdown report.",
        ),
    )
    llm.add(StepEvaluation, step_ok("Summary written / report saved"))
    llm.add(GoalEvaluation, goal_done("Saved the report to reports/top_branches.md."))
    client.post(
        f"/api/conversations/{conv}/messages",
        json={"content": "Now save a short report about the top 3 branches."},
    )
    wait_messages(client, conv, 4)

    # 2. Monthly gross profit trend (real data_summary output).
    llm.script.clear()
    llm.add(
        PlannerOutput,
        plan(
            "Monthly gross profit trend",
            ("Monthly gross profit trend with per-day averages", "data_summary", False),
        ),
    )
    llm.add(
        ToolArguments,
        args(
            '{"path": "demo/demo_sales.csv", "metric": "gross_profit", "date_column": "date"}',
            "Monthly gross profit with per-day averages.",
        ),
    )
    llm.add(
        StepEvaluation,
        step_ok(
            "Monthly totals and per-day averages computed",
            observations=["January 2026 is a partial month in the demo data"],
        ),
    )
    llm.add(
        GoalEvaluation,
        goal_done(
            "Gross profit was 1,311,625.06 for January-June 2026. June fell 7.6% against May, "
            "but only 4.6% per day, because June has one day fewer. May was the strongest "
            "complete month per day (7,448.42/day). January is partial and excluded."
        ),
    )
    trend = client.post(
        "/api/tasks",
        json={"goal": "Summarise the monthly gross profit trend in demo/demo_sales.csv"},
    ).json()["task_id"]
    wait(client, trend, {"COMPLETED"})

    # 3. A task waiting for approval (shows on the overview).
    llm.script.clear()
    llm.add(
        PlannerOutput,
        plan(
            "Send a dashboard notification",
            ("Notify the regional managers about the June dip", "notification", True),
        ),
    )
    llm.add(
        ToolArguments,
        args(
            '{"title": "June gross profit dip", "message": "Gross profit per day fell 4.6% in June '
            'vs May. Details in reports/top_branches.md.", "channel": "dashboard"}',
            "Short notice with the per-day figure and a link to the report.",
        ),
    )
    pending = client.post(
        "/api/tasks", json={"goal": "Tell the regional managers about the June gross profit dip"}
    ).json()["task_id"]
    wait(client, pending, {"WAITING_APPROVAL"})

    # 4. User notes in long-term memory.
    for content, category in [
        ("Gross profit in reports excludes VAT.", "finance"),
        ("Northgate is the flagship branch; compare other branches against it.", "branches"),
        ("Weekly MIS pack goes to the regional managers every Monday at 09:00.", "reporting"),
    ]:
        client.post("/api/memory", json={"content": content, "category": category})
    return trend


def shrink(folder: Path) -> None:
    """Reduce PNGs to 256 colours if Pillow is installed (about 4x smaller)."""
    try:
        from PIL import Image
    except ImportError:
        print("Pillow not installed: screenshots left unoptimised")
        return
    for path in sorted(folder.glob("*.png")):
        image = Image.open(path).convert("RGB")
        image.quantize(256, dither=Image.Dither.FLOYDSTEINBERG).save(path, optimize=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "screenshots")
    opts = parser.parse_args()
    opts.out.mkdir(parents=True, exist_ok=True)

    tmp = Path(tempfile.mkdtemp(prefix="dotlab-shots-"))
    settings = Settings(
        database_url=f"sqlite:///{tmp / 'demo.db'}",
        workspace_dir=tmp / "workspace",
        enable_monitor_scheduler=False,
        openai_model="demo-scripted-model",
        tool_permission_level="external",
        log_level="WARNING",
    )
    (settings.workspace_dir / "demo").mkdir(parents=True)
    shutil.copy(DEMO, settings.workspace_dir / "demo" / "demo_sales.csv")
    llm = DemoLLM()
    config = uvicorn.Config(create_app(settings, llm=llm), port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)

    try:
        with httpx.Client(base_url=BASE, timeout=30) as client:
            trend_task = seed(client, llm)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
            shots = [
                ("overview", "#overview", None),
                ("workspace", "#workspace", None),
                ("task-detail", "#history", trend_task),
                ("activity", "#activity", None),
                ("memory", "#memory", None),
                ("reports", "#reports", None),
                ("settings", "#settings", None),
            ]
            for name, hash_, task in shots:
                page.goto(f"{BASE}/{hash_}")
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(700)
                if task:
                    page.locator(f"[data-action=open-task][data-id='{task}']:visible").first.click()
                    page.wait_for_selector("#d-status.COMPLETED")
                    page.wait_for_timeout(400)
                    page.click("#d-content details summary >> nth=0")  # show step output
                    page.wait_for_timeout(400)
                page.screenshot(path=str(opts.out / f"{name}.png"))
                if task:
                    page.keyboard.press("Escape")  # close the drawer again
                print(f"saved {name}.png")
            mobile = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
            mobile.goto(f"{BASE}/#workspace")
            mobile.wait_for_load_state("networkidle")
            mobile.wait_for_timeout(700)
            mobile.screenshot(path=str(opts.out / "mobile.png"))
            print("saved mobile.png")
            browser.close()
    finally:
        server.should_exit = True
    shrink(opts.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
