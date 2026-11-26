"""Click through every dashboard section in a real browser (optional, not run in CI).

Starts Dot Lab in-process with the scripted fake LLM from the test suite (no API
key, no network), then uses Playwright to exercise each section: tasks, chat
with a clarification question, approvals, history, activity, memory, reports,
settings, monitors and the mobile layout. Fails on any JavaScript error.

    pip install playwright && playwright install chromium
    python scripts/ui_smoke_test.py [--screenshots DIR]
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402
from playwright.sync_api import Page, expect, sync_playwright  # noqa: E402

from app.agent.evaluator import GoalEvaluation, StepEvaluation  # noqa: E402
from app.agent.executor import ToolArguments  # noqa: E402
from app.agent.planner import PlannerOutput  # noqa: E402
from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.conftest import ScriptedLLM, args, goal_done, plan, question, step_ok  # noqa: E402

PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"


def script(llm: ScriptedLLM, *planner: PlannerOutput, result: str = "6*7 = 42") -> None:
    """Replace the fake LLM's script with one calculator task."""
    llm.script.clear()
    llm.add(PlannerOutput, *(planner or (plan(("Compute 6*7", "calculator", False)),)))
    llm.add(ToolArguments, args('{"expression": "6*7"}'))
    llm.add(StepEvaluation, step_ok(observations=["6*7 is 42"]))
    llm.add(GoalEvaluation, goal_done(result))


def step(name: str) -> None:
    print(f"  ok  {name}")


def run(page: Page, llm: ScriptedLLM, workspace: Path, shots: Path | None) -> None:
    def shot(name: str) -> None:
        if shots:
            page.screenshot(path=str(shots / f"{name}.png"), full_page=False)

    page.on("dialog", lambda d: d.accept())  # confirm()/prompt() dialogs

    # Overview -------------------------------------------------------------
    page.goto(f"{BASE}/#overview")
    expect(page.locator("#kpis .kpi")).to_have_count(8)
    expect(page.locator("#llm-status")).to_contain_text("AI")
    step("overview loads with 8 KPI cards")

    # New task via modal ----------------------------------------------------
    script(llm)
    page.click("#new-task-btn")
    page.fill("#modal-goal", "What is 6 times 7?")
    page.click("#modal-task-form button[type=submit]")
    expect(page.locator("#d-status")).to_have_text("COMPLETED", timeout=15000)
    expect(page.locator("#d-content")).to_contain_text("6*7 = 42")
    step("new task modal -> drawer shows COMPLETED result")
    with page.expect_download() as dl:
        page.click("#d-actions [data-action=task-report]")
    assert dl.value.suggested_filename.startswith("dot-lab-task-")
    page.keyboard.press("Escape")
    expect(page.locator("#drawer")).to_be_hidden()
    step("task report download + Escape closes drawer")

    # Approval flow from the overview ---------------------------------------
    script(llm, plan(("Compute 6*7 (sensitive)", "calculator", True)), result="approved 42")
    page.goto(f"{BASE}/#tasks")
    page.fill("#goal", "Sensitive calculation")
    page.click("#task-form button[type=submit]")
    expect(page.locator("#d-status")).to_have_text("WAITING APPROVAL", timeout=15000)
    page.keyboard.press("Escape")
    page.goto(f"{BASE}/#overview")
    expect(page.locator("#attention")).to_contain_text("Needs your attention", timeout=10000)
    expect(page.locator("#count-attention")).to_have_text("1")
    shot("01-overview-attention")
    page.click("#attention [data-action=approve]")
    expect(page.locator("#attention")).to_be_empty(timeout=15000)
    step("approval card on overview -> approved -> attention cleared")

    # Tasks view: filter + monitors -----------------------------------------
    page.goto(f"{BASE}/#tasks")
    expect(page.locator("#task-rows tr.clickable")).to_have_count(2, timeout=10000)
    page.select_option("#status-filter", "COMPLETED")
    expect(page.locator("#task-rows tr.clickable")).to_have_count(2, timeout=10000)
    page.select_option("#status-filter", "FAILED")
    expect(page.locator("#task-rows")).to_contain_text("No tasks match")
    page.select_option("#status-filter", "")
    script(llm)
    page.fill("#m-name", "Daily check")
    page.fill("#m-goal", "Compute 6*7 daily")
    page.fill("#m-interval", "60")
    page.click("#monitor-form button[type=submit]")
    expect(page.locator("#monitor-list")).to_contain_text("Daily check", timeout=10000)
    page.click("#monitor-list [data-action=monitor-toggle]")
    expect(page.locator("#monitor-list")).to_contain_text("DISABLED", timeout=10000)
    shot("03-tasks")
    page.click("#monitor-list [data-action=monitor-delete]")
    expect(page.locator("#monitor-list")).to_contain_text("No monitors yet", timeout=10000)
    step("tasks: status filter, monitor create/disable/delete")

    # AI Workspace: question + answer in a conversation ----------------------
    script(
        llm,
        question("Which month should I use?"),
        plan(("Compute", "calculator", False)),
        result="June total: 42",
    )
    page.goto(f"{BASE}/#workspace")
    page.click("[data-action=suggest] >> nth=0")
    expect(page.locator("#chat-input")).not_to_have_value("")
    page.fill("#chat-input", "Report revenue for the branches")
    page.press("#chat-input", "Enter")
    expect(page.locator("#chat-messages")).to_contain_text(
        "Which month should I use?", timeout=15000
    )
    expect(page.locator("#chat-messages")).to_contain_text("next message answers", timeout=10000)
    page.fill("#chat-input", "June 2026")
    page.click("#chat-send")
    expect(page.locator("#chat-messages .msg")).to_have_count(4, timeout=15000)
    expect(page.locator("#chat-messages")).to_contain_text("June total: 42")
    expect(page.locator("#conv-items .conv")).to_have_count(1)
    shot("02-workspace")
    step("chat: suggestion, question asked, answered by next message, reply shown")

    # History ---------------------------------------------------------------
    page.goto(f"{BASE}/#history")
    # 3 tasks: the new monitor only schedules its first run (the scheduler is off here).
    expect(page.locator("#history-rows tr.clickable")).to_have_count(3, timeout=10000)
    page.fill("#history-q", "sensitive")
    expect(page.locator("#history-rows tr.clickable")).to_have_count(1, timeout=10000)
    with page.expect_download():
        page.click("#history-rows [data-action=task-report]")
    page.fill("#history-q", "")
    shot("04-history")
    step("history: search + report download")

    # Activity --------------------------------------------------------------
    page.goto(f"{BASE}/#activity")
    expect(page.locator("#activity-list")).to_contain_text("TASK_COMPLETED", timeout=10000)
    page.click("[data-kind=approvals]")
    expect(page.locator("#activity-list")).to_contain_text("APPROVAL_GRANTED", timeout=10000)
    page.click("[data-kind=errors]")
    expect(page.locator("#activity-list")).to_contain_text("No activity for this filter")
    page.click("[data-kind=all]")
    shot("05-activity")
    step("activity: feed + filters")

    # Memory ----------------------------------------------------------------
    page.goto(f"{BASE}/#memory")
    page.fill("#memory-content", "Gross profit in reports excludes VAT")
    page.fill("#memory-category", "Finance")
    page.click("#memory-form button[type=submit]")
    expect(page.locator("#memory-categories")).to_contain_text("finance", timeout=10000)
    page.click("[data-action=memory-category][data-category=finance]")
    expect(page.locator("#memory-list .memory-card")).to_have_count(1, timeout=10000)
    page.click("#memory-list [data-action=memory-edit]")
    page.fill("#edit-category", "accounting")
    page.click("#memory-edit-form button[type=submit]")
    expect(page.locator("#memory-categories")).to_contain_text("accounting", timeout=10000)
    page.click("[data-action=memory-category][data-category='']")
    page.fill("#memory-q", "VAT")
    expect(page.locator("#memory-list .memory-card")).to_have_count(1, timeout=10000)
    shot("06-memory")
    page.click("#memory-list [data-action=memory-delete]")
    expect(page.locator("#memory-list")).to_contain_text("Nothing matches", timeout=10000)
    step("memory: add note, category filter, edit, search, delete")

    # Reports ---------------------------------------------------------------
    reports = workspace / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "branch_profit.md").write_text(
        "# Branch profit\n\nNorthgate leads.\n", encoding="utf-8"
    )
    page.goto(f"{BASE}/#reports")
    expect(page.locator("#report-files")).to_contain_text("branch_profit.md", timeout=10000)
    page.click("#report-files [data-action=view-report]")
    expect(page.locator("#modal-body")).to_contain_text("Northgate leads.")
    with page.expect_download() as dl:
        page.click("#modal-body [data-action=download-report]")
    assert dl.value.suggested_filename == "branch_profit.md"
    page.keyboard.press("Escape")
    expect(page.locator("#report-tasks .item")).to_have_count(3)
    shot("07-reports")
    step("reports: saved report view + download, task reports listed")

    # Settings --------------------------------------------------------------
    page.goto(f"{BASE}/#settings")
    expect(page.locator("#settings-tools tr")).to_have_count(8, timeout=10000)
    expect(page.locator("#settings-config")).to_contain_text("Tool permission level")
    page.fill("#api-key", "local-test-key")
    page.click("#api-key-form button[type=submit]")
    expect(page.locator("#api-key")).to_have_attribute("placeholder", "Key saved in this browser")
    page.click("[data-action=clear-api-key]")
    shot("08-settings")
    step("settings: config, 8 tools, API key save/clear")

    # Mobile layout ----------------------------------------------------------
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{BASE}/#overview")
    expect(page.locator("#menu-toggle")).to_be_visible()
    page.click("#menu-toggle")
    expect(page.locator("#sidebar")).to_have_class("sidebar open")
    page.click("[data-nav=workspace]")
    expect(page.locator("#view-workspace")).to_be_visible()
    expect(page.locator("#sidebar")).to_have_class("sidebar")  # menu closes after navigating
    expect(page.locator("[data-nav=workspace]")).to_have_class("active")
    page.click("#menu-toggle")
    expect(page.locator("#sidebar")).to_have_class("sidebar open")
    page.mouse.click(370, 600)  # tap outside the menu
    expect(page.locator("#sidebar")).to_have_class("sidebar")
    page.wait_for_timeout(400)  # let the slide-out animation finish before the screenshot
    width = page.evaluate("document.documentElement.scrollWidth")
    assert width <= 390, f"horizontal scroll on mobile ({width}px)"
    shot("09-mobile-workspace")
    step("mobile: menu opens, navigation works, no horizontal scroll")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshots", type=Path, default=None)
    opts = parser.parse_args()
    if opts.screenshots:
        opts.screenshots.mkdir(parents=True, exist_ok=True)

    tmp = Path(tempfile.mkdtemp(prefix="dotlab-ui-"))
    settings = Settings(
        database_url=f"sqlite:///{tmp / 'ui.db'}",
        workspace_dir=tmp / "workspace",
        enable_monitor_scheduler=False,
        log_level="WARNING",
    )
    llm = ScriptedLLM()
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings, llm=llm), port=PORT, log_level="warning")
    )
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)

    errors: list[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900}, accept_downloads=True)
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            run(page, llm, settings.workspace_dir, opts.screenshots)
            browser.close()
    finally:
        server.should_exit = True
    if errors:
        print("JavaScript errors:\n  " + "\n  ".join(errors))
        return 1
    print("UI smoke test passed (no JavaScript errors).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
