# Roadmap

Dot Lab is an educational and research project, so the roadmap favours ideas that teach something about agents over features for their own sake. Items are not promises or dates. Suggestions are welcome as [feature requests](https://github.com/Anjub004/dot-lab/issues/new/choose).

## 1.0 (this release)

- Goal → plan → act → review loop with structured outputs and bounded retries, re-planning and timeouts
- Tools with permission levels: calculator, web search, browser, file system, notifications, CSV analysis, data summaries, Markdown reports
- Human approvals and clarification questions
- Long-term memory with categories, and conversations for short-term context
- Token and cost budgets per task, model availability check
- Monitoring mode (experimental)
- Dashboard with 8 sections
- Alembic migrations, offline test suite, Docker image

## Next (2.x ideas)

- **Better memory**: semantic search (embeddings / vector store) behind the existing `MemoryStore` interface, plus memory expiry.
- **More tools**: Excel files, charts in reports, read-only SQL over a configured database.
- **Durable execution**: a task queue (e.g. Arq or RQ) instead of the in-process asyncio runner, so restarts never repeat a step.
- **Better scheduling**: cron expressions and quiet hours for monitors.
- **Agent evaluation**: a benchmark of goals with expected outcomes, run against real models to catch regressions in prompts.
- **Streaming**: live token output in the AI Workspace.

## Later (3.x ideas)

- Pluggable model providers behind the existing `LLMClient` protocol
- Observability: OpenTelemetry traces and metrics
- Multi-user mode with accounts and per-user workspaces
- Multi-agent experiments (a planner agent delegating to specialist agents)

## Known limitations of 1.0

- Background execution uses asyncio inside the API process. It is not durable; interrupted tasks are re-queued on restart and an interrupted step may run again.
- SQLite with synchronous SQLAlchemy calls from async code: fine for a single local user, not for high concurrency.
- `TASK_TIMEOUT_SECONDS` applies to each run segment; time spent waiting for approval, an answer, or paused is not counted.
- Memory search is keyword-based, not semantic.
- Migrations are tested on SQLite only.
- Budgets are checked before each LLM call, so a task can exceed its limit by at most one call. Monitor change-comparison calls run outside a task and are not counted.
- No user accounts; a single shared optional API key.
- An LLM error fails the task (after the SDK's own retries) rather than attempting recovery.
- The monitor scheduler runs in the API process: run one instance per database.
