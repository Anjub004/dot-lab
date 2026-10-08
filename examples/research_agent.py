"""Example 1 — research agent.

Goal: research the current state of AI agents and create a summary.

For real research configure a search provider in .env:

    SEARCH_PROVIDER=tavily   # or brave
    SEARCH_API_KEY=...

Without one, the planner is told web search is unavailable and the agent will
fall back to the browser tool (on URLs it knows) and to reasoning; its answer
will say what it could not verify.

    python examples/research_agent.py
    python examples/research_agent.py --goal "Research recent open-source agent frameworks"
"""

from __future__ import annotations

import argparse
import asyncio

from _common import build, print_task, run_goal

GOAL = (
    "Research the current state of AI agents: find recent developments from authoritative "
    "sources, identify the major trends, and create a concise summary with source links. "
    "Save the summary to reports/ai_agents_summary.md."
)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--goal", default=GOAL)
    parser.add_argument("--yes", action="store_true", help="auto-approve every request")
    options = parser.parse_args()

    ctx = build()
    if not ctx.tools.get("web_search").availability().available:
        print("Note: web search is not configured (SEARCH_PROVIDER/SEARCH_API_KEY).")
    task = await run_goal(ctx, options.goal, auto_approve="y" if options.yes else None)
    print_task(ctx, task)
    await ctx.runner.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
