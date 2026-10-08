"""Example 3 — a simple multi-step task.

Goal: calculate statistics for a set of values and save them to the workspace.
Uses the calculator and filesystem tools (no web search needed).

    python examples/simple_task.py
    python examples/simple_task.py --yes        # auto-approve (e.g. file overwrite)
"""

from __future__ import annotations

import argparse
import asyncio

from _common import build, print_task, run_goal

GOAL = (
    "Calculate the sum, mean, minimum and maximum of these values: 12, 45, 7, 23, 56. "
    "Then save the results as a short markdown table to results/stats.md."
)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--goal", default=GOAL)
    parser.add_argument("--yes", action="store_true", help="auto-approve every request")
    options = parser.parse_args()

    ctx = build()
    task = await run_goal(ctx, options.goal, auto_approve="y" if options.yes else None)
    print_task(ctx, task)
    print(f"\nWorkspace: {ctx.settings.workspace_dir}")
    await ctx.runner.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
