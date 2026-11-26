# Changelog

All notable changes to Dot Lab are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.0.0] - 2026-11-26

First stable release. Databases created by 0.1.0 are upgraded automatically on start-up, without losing data.

### Added

- **Reviewer step and honest results**: a new `REVIEWING` status; a task is `COMPLETED` only when the reviewer confirms the goal is satisfied, otherwise it is `FAILED` with the partial result kept.
- **Model check**: on start-up and in `GET /api/health`, Dot Lab checks that `OPENAI_MODEL` is available to the API key.
- **Token and cost budgets**: token usage of every LLM call is recorded per task (`MAX_TASK_TOKENS`, optional `MAX_TASK_COST_USD` with your own prices) and shown in the task detail.
- **Data analysis tools**: `csv_analyzer` (profile, aggregate, top rows with structured filters), `data_summary` (totals, day/week/month trends with partial periods flagged and per-day averages, rankings) and `report_generator` (Markdown reports in `reports/`), plus synthetic demo sales data and `examples/seed_demo_data.py`.
- **Tool permission levels** (`read`, `write`, `external`) with `TOOL_PERMISSION_LEVEL`, and per-tool timeouts capped by `TOOL_TIMEOUT_SECONDS`.
- **Conversations**: chat-style follow-ups where earlier messages are context for the planner and reviewer, and results are posted back as replies.
- **Memory management**: categories, keyword search, user notes, editing and deleting through `/api/memory`. Memory now holds results, observations and notes; goals, plans and raw tool output stay in the execution log.
- **Clarification questions**: when essential information is missing, the planner asks (at most two questions per task) instead of guessing; new `WAITING_INPUT` status and `POST /api/tasks/{id}/answer`.
- **Task reports**: any task can be downloaded as a Markdown report (`GET /api/tasks/{id}/report`).
- **Database migrations** with Alembic, applied on start-up, with foreign-key-safe table rebuilds on SQLite.
- **Dashboard** rebuilt as an 8-section workspace: Overview, AI Workspace (chat), Tasks, Execution History, Agent Activity, Memory, Reports and Settings, with a task drawer, live updates and a phone layout.
- **API**: `/api/activity`, `/api/settings`, `/api/tools`, `/api/reports`, task search and filters.
- **Live tests** against the real OpenAI API (`pytest -m live`, never run by default or in CI) and a browser smoke test for the dashboard (`scripts/ui_smoke_test.py`).
- **Open-source project files**: README screenshots generated from the real app (`scripts/make_screenshots.py`), usage, configuration and roadmap guides in `docs/`, Code of Conduct, issue and pull request templates, Dependabot, and this changelog.
- CI runs on Python 3.12 and 3.13, and builds and starts the Docker image.

### Changed

- Docker image installs runtime dependencies only, and a `.dockerignore` keeps `.env`, local data and development files out of the build.
- `SECURITY.md` now lists supported versions, what is in scope, and the protections for data tools, permission levels and the dashboard.
- The README is shorter; detailed usage and configuration moved to `docs/`.

### Fixed

- Memory search escapes `%` and `_` so they match literally.
- Tasks interrupted while `REVIEWING` are recovered after a restart.
- Dashboard: no horizontal scrolling on phone-width screens; long task goals and conversation titles wrap or shorten instead of widening the page.
- Plan events say "1 step" instead of "1 steps".

## [0.1.0] - 2026-10-08

Initial public version.

### Added

- Goal → plan → act → evaluate agent loop with structured outputs (OpenAI Responses API), iteration limit, task and tool timeouts, bounded retries and re-planning.
- Tools: calculator (safe AST evaluator), web search (Tavily, Brave or none), read-only SSRF-protected browser, workspace-confined file system, notifications (dashboard and webhook).
- Human approval for sensitive actions, with the exact approved input executed.
- Persistent SQLite memory and task history; background execution with pause, resume, cancel and restart recovery.
- Experimental monitoring mode that notifies only on meaningful change.
- Dashboard, REST API, JSON logging with secret redaction, optional API key.
- Offline test suite, CI, Docker support and examples.

[Unreleased]: https://github.com/Anjub004/dot-lab/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/Anjub004/dot-lab/releases/tag/v1.0.0
[0.1.0]: https://github.com/Anjub004/dot-lab/tree/41309de
