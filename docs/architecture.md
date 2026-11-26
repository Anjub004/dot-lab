# Dot Lab architecture

This document describes how Dot Lab is built today and how it maps to the
long-term design (orchestrator, planner, executor, reviewer, tools, memory).
It is updated at the end of every milestone.

## Components

| Concept | Module | Responsibility |
|---|---|---|
| Orchestrator | `app/agent/agent.py` (`Agent`) | Drives one task through its lifecycle; all state is stored in SQLite so a task can resume after an approval, pause or restart. |
| Planner | `app/agent/planner.py` | Turns a goal into a structured plan (OpenAI Structured Outputs) and validates every step against the tool registry. |
| Executor | `app/agent/executor.py` | Generates tool arguments, validates them against the tool's Pydantic input model, runs the tool with a timeout and bounded retries, and runs reasoning steps. |
| Reviewer | `app/agent/evaluator.py` (`Evaluator`) | Judges each step (continue / retry / finish / fail) and reviews the whole result at the end. |
| LLM provider | `app/agent/llm.py` | `LLMClient` protocol, `OpenAILLM` implementation, model availability check. Other providers implement the same protocol. |
| Tool registry | `app/tools/` | `BaseTool` interface and the registered tools. The agent can only call tools in the registry. |
| Approvals | `app/approvals/manager.py` | Human-in-the-loop approval requests and decisions. |
| Long-term memory | `app/memory/` | `MemoryStore` interface and `SQLiteMemoryStore` (keyword search). |
| Execution log | `app/services/task_service.py` | Tasks, plan, events and tool calls (the audit trail). |
| Runner / monitors | `app/services/runner.py`, `monitoring.py` | Background execution, restart recovery, scheduled goals. |
| API / dashboard | `app/api/`, `dashboard/` | FastAPI routes and the web interface. |

The folder names differ from the original target layout (`app/core/…`).
They were kept on purpose: renaming would touch every import and test without
adding functionality.

## Tools and permissions

| Tool | Permission | Own timeout | Purpose |
|---|---|---|---|
| `calculator` | read | 5 s | Safe arithmetic (AST allow-list, no `eval`) |
| `csv_analyzer` | read | 20 s | Profile / aggregate / top rows of a workspace CSV |
| `data_summary` | read | 20 s | Totals, period trend (partial periods flagged), ranking |
| `filesystem` | write | global | Read / write / list inside the workspace |
| `report_generator` | write | 10 s | Save a Markdown report to `reports/` |
| `web_search` | external | global | Search provider (Tavily / Brave) |
| `browser` | external | global | Read public web pages (SSRF-protected) |
| `notification` | external | global | Dashboard notification or webhook (approval) |

- `TOOL_PERMISSION_LEVEL` (read < write < external) removes higher tools from
  the planner's tool list, and `ToolRegistry.get()` refuses them, so a plan
  that names one fails safely.
- A tool's own timeout can only make the limit shorter:
  effective timeout = `min(tool.timeout_seconds, TOOL_TIMEOUT_SECONDS)`.
- The data tools take structured parameters (operation, columns, metrics,
  filters) validated by Pydantic. No `eval`, `DataFrame.query` or code strings
  from the model are executed. pandas work runs in a worker thread so the API
  stays responsive. A timed-out thread finishes in the background, but its
  result is discarded.

## Task lifecycle

```mermaid
stateDiagram-v2
    [*] --> PENDING
    PENDING --> PLANNING
    PLANNING --> RUNNING
    RUNNING --> WAITING_APPROVAL: sensitive tool call
    WAITING_APPROVAL --> PENDING: approved / rejected
    RUNNING --> REVIEWING: all steps finished
    REVIEWING --> RUNNING: reviewer adds steps (bounded)
    REVIEWING --> COMPLETED: goal satisfied
    REVIEWING --> FAILED: goal not satisfied (partial result kept)
    RUNNING --> FAILED: error / limit reached
    PENDING --> PAUSED
    RUNNING --> PAUSED
    REVIEWING --> PAUSED
    PAUSED --> PENDING: resume
    COMPLETED --> [*]
    FAILED --> [*]
    CANCELLED --> [*]
```

Pause and cancel are allowed from every non-final state. Transitions are
enforced in `app/agent/state.py`; anything not listed there is rejected.

**A task is COMPLETED only when the reviewer confirms the goal is satisfied.**
If it is not, the task is FAILED and the partial output is kept in `result`
with a "Partial result" note.

## Memory vs execution log

Two separate stores, with different purposes:

| | Execution log | Long-term memory |
|---|---|---|
| Tables | `tasks`, `task_events`, `tool_calls`, `approvals` | `memories` |
| Contains | Goal, plan, every event, every tool input and output | Reviewer observations and final results of completed tasks |
| Purpose | Audit trail and dashboard history | Context for planning future tasks |
| Written | Automatically, always | Only for useful knowledge |

Raw tool output, goals and plans are **not** copied into long-term memory.
Rows of kind `goal`, `plan` and `tool_result` written by earlier versions stay
in the database untouched; the planner never searches those kinds.

## Safety limits

`MAX_AGENT_ITERATIONS`, `MAX_PLAN_STEPS`, `MAX_STEP_RETRIES`, `MAX_REPLANS`,
`TOOL_TIMEOUT_SECONDS`, `TASK_TIMEOUT_SECONDS` and `MAX_CONCURRENT_TASKS` are
set in `.env`.

### Token and cost budgets

```text
Agent.run(task)
  budget = totals of earlier runs (llm_usage) + limits from .env
  with use_budget(budget):              # ContextVar: visible to every LLM call
      planner / executor / reviewer -> OpenAILLM.generate()
          budget.check()                # over the limit? BudgetExceededError, no request sent
          responses.parse(...)
          budget.record(response.usage) # one llm_usage row per call
  BudgetExceededError -> task FAILED with "Token budget exceeded ..." / "Cost budget exceeded ..."
```

- `MAX_TASK_TOKENS` (default 200,000; 0 = unlimited) counts input + output tokens.
- `MAX_TASK_COST_USD` is optional and needs `OPENAI_INPUT_COST_PER_1M` and
  `OPENAI_OUTPUT_COST_PER_1M`. Prices change, so Dot Lab never hard-codes them;
  the cost shown is an estimate.
- The check happens before each call, so the limit can be exceeded by at most
  one call.
- Usage is stored in its own `llm_usage` table. Adding a table needs no
  migration: `create_all()` creates it in existing databases.

## Startup checks

On startup Dot Lab checks in the background that `OPENAI_MODEL` is available
to the configured API key (short timeout, no retries). The result is shown in
`GET /api/health` as `model_check`:

| status | Meaning |
|---|---|
| `available` | The model exists and the key can use it. |
| `unavailable` | Wrong model name, no access, or the key was rejected. |
| `error` | The check could not reach OpenAI (network). Tasks may still work. |
| `unchecked` | OpenAI is not configured, or the check has not run yet. |
