"""Shared helpers for the example scripts.

The examples run Dot Lab *in-process* (no web server needed): they build the
same application context the API uses, submit a goal to the background runner,
and ask for approval in the terminal whenever the agent needs it.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agent.state import TERMINAL_STATUSES, TaskStatus  # noqa: E402
from app.approvals.manager import ApprovalStatus  # noqa: E402
from app.config import Settings  # noqa: E402
from app.context import AppContext, build_context  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.services.task_service import TaskView  # noqa: E402

__all__ = ["TaskStatus", "ask_approval", "build", "print_task", "run_goal"]


def build() -> AppContext:
    """Build the app context from .env, exiting with a clear message if OpenAI is missing."""
    settings = Settings.from_env()
    configure_logging("WARNING", settings.secret_values(), json_output=False)
    if not settings.llm_configured:
        sys.exit(
            "OpenAI is not configured. Copy .env.example to .env and set "
            "OPENAI_API_KEY and OPENAI_MODEL."
        )
    return build_context(settings)


def ask_approval(ctx: AppContext, task_id: str, auto: str | None) -> None:
    """Show pending approvals and record a decision (interactive unless auto is set)."""
    for approval in ctx.approvals.list(status=ApprovalStatus.PENDING, task_id=task_id):
        print("\n=== Agent is waiting for your approval ===")
        print(f"Action:      {approval.action}")
        print(f"Reason:      {approval.reason}")
        print(f"Explanation: {approval.explanation}")
        print(f"Tool input:  {approval.input}")
        if auto is None:
            answer = input("Approve? [y/N] ").strip().lower()
        else:
            answer = auto
            print(f"(auto-answer: {answer})")
        ctx.approvals.decide(approval.id, approved=answer in {"y", "yes"})
    ctx.tasks.try_transition(task_id, {TaskStatus.WAITING_APPROVAL}, TaskStatus.PENDING)


async def run_goal(ctx: AppContext, goal: str, auto_approve: str | None = None) -> TaskView:
    """Run a goal to completion in the background runner, handling approvals."""
    task = ctx.tasks.create_task(goal)
    print(f"Task {task.id} created: {goal}")
    while True:
        ctx.runner.submit(task.id)
        await ctx.runner.wait(task.id)
        task = ctx.tasks.get_task(task.id)
        if task.status is TaskStatus.WAITING_APPROVAL:
            ask_approval(ctx, task.id, auto_approve)
            continue
        if task.status in TERMINAL_STATUSES or task.status is TaskStatus.PAUSED:
            return task


def print_task(ctx: AppContext, task: TaskView) -> None:
    print(f"\nStatus: {task.status}")
    if task.plan:
        print("\nPlan:")
        for step in task.plan.steps:
            tool = step.tool or "reasoning"
            print(f"  [{step.status:<16}] {step.id} ({tool}): {step.description}")
    print("\nTool calls:")
    for call in ctx.tasks.list_tool_calls(task.id):
        print(f"  {call.tool:<12} {call.status:<7} {call.input}")
    if task.error:
        print(f"\nError: {task.error}")
    if task.result:
        print("\n=== Final result ===\n")
        print(task.result)
