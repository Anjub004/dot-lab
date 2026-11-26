"""Task report: a Markdown summary of one task for people to read or share.

The report contains the goal, clarifications, outcome, plan, tool calls,
approvals, token usage and the timeline. Tool outputs are shortened, because
they can be large and come from untrusted sources (web pages, files).
"""

from __future__ import annotations

from datetime import datetime

from app.agent.usage import UsageReport
from app.approvals.manager import Approval
from app.services.task_service import TaskEvent, TaskView, ToolCall

OUTPUT_PREVIEW_CHARS = 800


def build_task_report(
    task: TaskView,
    *,
    events: list[TaskEvent],
    tool_calls: list[ToolCall],
    approvals: list[Approval],
    usage: UsageReport,
    generated_at: datetime,
) -> str:
    lines: list[str] = [
        "# Dot Lab task report",
        "",
        f"_Generated {_time(generated_at)}_",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Task ID | `{task.id}` |",
        f"| Status | **{task.status}** |",
        f"| Created | {_time(task.created_at)} |",
        f"| Started | {_time(task.started_at)} |",
        f"| Finished | {_time(task.completed_at)} |",
        f"| Duration | {_duration(task.started_at, task.completed_at)} |",
        f"| Iterations | {task.iterations} |",
        f"| LLM calls | {usage.llm_calls} |",
        f"| Tokens | {usage.total_tokens:,} ({usage.input_tokens:,} in / "
        f"{usage.output_tokens:,} out) |",
    ]
    if usage.estimated_cost_usd is not None:
        lines.append(f"| Estimated cost | ${usage.estimated_cost_usd:.4f} (estimate) |")

    lines += ["", "## Goal", "", task.goal.strip(), ""]

    if task.clarifications:
        lines += ["## Clarifications", ""]
        for c in task.clarifications:
            lines.append(f"- **Q:** {_inline(c.question)}")
            lines.append(f"  **A:** {_inline(c.answer) if c.answer else '_not answered_'}")
        lines.append("")

    if task.error:
        lines += ["## Error", "", task.error.strip(), ""]
    lines += ["## Result", "", (task.result or "_No result._").strip(), ""]

    lines += ["## Plan", ""]
    if task.plan and task.plan.steps:
        if task.plan.summary:
            lines += [task.plan.summary.strip(), ""]
        lines += ["| # | Step | Tool | Status | Attempts |", "|---|---|---|---|---|"]
        for step in task.plan.steps:
            lines.append(
                f"| {step.id} | {_cell(step.description)} | {step.tool or 'reasoning'} | "
                f"{step.status} | {step.attempts} |"
            )
    else:
        lines.append("_No plan was created._")
    lines.append("")

    lines += ["## Tool calls", ""]
    if tool_calls:
        for call in tool_calls:
            lines.append(
                f"### {call.step_id} · `{call.tool}` · {call.status} "
                f"(attempt {call.attempt}, {call.duration_ms} ms)"
            )
            if call.error:
                lines += ["", f"Error: {_inline(call.error)}"]
            if call.output:
                preview = call.output[:OUTPUT_PREVIEW_CHARS]
                more = len(call.output) - len(preview)
                lines += ["", "```text", preview.replace("```", "'''"), "```"]
                if more > 0:
                    lines.append(f"_{more:,} more characters not shown._")
            lines.append("")
    else:
        lines += ["_No tools were called._", ""]

    lines += ["## Approvals", ""]
    if approvals:
        lines += ["| Action | Reason | Decision | Note |", "|---|---|---|---|"]
        for a in approvals:
            lines.append(
                f"| {_cell(a.action)} | {_cell(a.reason)} | {a.status} | "
                f"{_cell(a.decision_note or '')} |"
            )
    else:
        lines.append("_No approvals were needed._")
    lines.append("")

    lines += ["## Timeline", ""]
    for e in events:
        lines.append(f"- {_time(e.created_at)} · **{e.event}** · {_inline(e.message)}")
    lines.append("")
    return "\n".join(lines)


def _time(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S UTC") if value else "-"


def _duration(start: datetime | None, end: datetime | None) -> str:
    if not start or not end:
        return "-"
    seconds = int((end - start).total_seconds())
    minutes, seconds = divmod(max(seconds, 0), 60)
    return f"{minutes} min {seconds} s" if minutes else f"{seconds} s"


def _inline(text: str) -> str:
    return " ".join(text.split())[:1000]


def _cell(text: str) -> str:
    return _inline(text).replace("|", "\\|")[:300]
