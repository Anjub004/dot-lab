"""Example 2 — monitoring agent.

Creates a monitor and runs a few cycles back to back so you can watch the
compare-and-notify behaviour without waiting hours:

1. cycle 1 establishes a baseline (no notification),
2. later cycles are compared with the previous result by the LLM,
3. a dashboard notification is created only for a meaningful change.

In the server, the same monitor would be triggered automatically by the
scheduler every ``interval_minutes``.

    python examples/monitoring_agent.py --cycles 2 --pause 60
"""

from __future__ import annotations

import argparse
import asyncio

from _common import TaskStatus, ask_approval, build

GOAL = (
    "Check for meaningful new developments in open-source AI agent frameworks and "
    "summarise them in 5 bullet points with sources."
)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="AI Agents Monitor")
    parser.add_argument("--goal", default=GOAL)
    parser.add_argument("--interval", type=int, default=360, help="minutes (for the scheduler)")
    parser.add_argument("--cycles", type=int, default=2)
    parser.add_argument("--pause", type=int, default=60, help="seconds between demo cycles")
    options = parser.parse_args()

    ctx = build()
    monitor = ctx.monitors.create(options.name, options.goal, options.interval, run_now=False)
    print(f"Monitor {monitor.id} created: {monitor.name}")

    for cycle in range(1, options.cycles + 1):
        print(f"\n--- cycle {cycle} ---")
        task_id = ctx.monitors.trigger(monitor.id)
        if task_id is None:
            print("Previous run still in progress; skipping.")
            continue
        while True:
            await ctx.runner.wait(task_id)
            task = ctx.tasks.get_task(task_id)
            if task.status is not TaskStatus.WAITING_APPROVAL:
                break
            ask_approval(ctx, task_id, auto=None)
            ctx.runner.submit(task_id)
        print(f"Task {task_id}: {task.status}")
        current = ctx.monitors.get(monitor.id)
        print(f"Change check: {current.last_change_summary}")
        if cycle < options.cycles:
            print(f"Sleeping {options.pause}s until the next cycle...")
            await asyncio.sleep(options.pause)

    print("\nNotifications:")
    for note in ctx.notifications.list():
        print(f"- {note.title}: {note.message}")
    await ctx.runner.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
