# Using Dot Lab

How to run tasks, conversations, monitors, approvals and reports, from the dashboard and from the HTTP API. All examples assume the server runs at `http://127.0.0.1:8000`. If you set `DOT_LAB_API_KEY`, add `-H "X-API-Key: <your key>"` to every `curl` call.

- [Your first task](#your-first-task)
- [Example workflow](#example-workflow)
- [Data analysis](#data-analysis)
- [Conversations](#conversations)
- [Long-term memory](#long-term-memory)
- [Clarification questions](#clarification-questions)
- [Human approval](#human-approval)
- [Task reports](#task-reports)
- [Monitoring](#monitoring)
- [Running without the server](#running-without-the-server)

The interactive API reference is at <http://127.0.0.1:8000/docs>.

## Your first task

From the dashboard: press **New task**, type a goal and press **Start task**. The task drawer opens and updates live. Or open **AI Workspace** and send the goal as a chat message.

From the command line:

```bash
curl -s -X POST http://127.0.0.1:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"goal": "Research the latest developments in AI agents and create a summary."}'
```

```json
{
  "task_id": "3f0c9c7e5b8a4c1f9a1e2d3c4b5a6978",
  "goal": "Research the latest developments in AI agents and create a summary.",
  "status": "PENDING",
  "created_at": "2026-10-07T09:12:44.501Z"
}
```

Follow and control it:

```bash
curl -s http://127.0.0.1:8000/api/tasks/<task_id>          # full detail
curl -s http://127.0.0.1:8000/api/tasks                    # list (?status=RUNNING, ?q=revenue, ?finished=true)
curl -s http://127.0.0.1:8000/api/overview                 # counts
curl -s "http://127.0.0.1:8000/api/activity?kind=errors"   # event feed: all|errors|approvals|questions|tools|tasks
curl -s -X POST http://127.0.0.1:8000/api/tasks/<task_id>/pause
curl -s -X POST http://127.0.0.1:8000/api/tasks/<task_id>/resume
curl -s -X POST http://127.0.0.1:8000/api/tasks/<task_id>/cancel
```

Statuses: `PENDING`, `PLANNING`, `WAITING_INPUT`, `RUNNING`, `WAITING_APPROVAL`, `REVIEWING`, `PAUSED`, `COMPLETED`, `FAILED`, `CANCELLED`. See [architecture.md](architecture.md) for the full lifecycle.

## Example workflow

1. You submit *"Research the latest developments in AI agents, summarise them, and post the summary to our team webhook."*
2. Status `PLANNING`: the planner returns e.g. `web_search` → `browser` → `browser` → *compare findings* (reasoning) → *write summary* (reasoning) → `notification` (webhook, `requires_approval: true`).
3. Status `RUNNING`: each step runs; tool calls, results and observations appear in the task drawer.
4. Before the webhook step the task becomes `WAITING_APPROVAL` and the dashboard shows the action, reason, the agent's explanation and the exact payload under **Needs your attention**.
5. You press **Approve** (or **Reject**). The task resumes and sends exactly what you approved (or skips that step).
6. Status `REVIEWING`: the reviewer checks the goal is satisfied and the task becomes `COMPLETED` with the final summary (or `FAILED` with the partial result if it is not). Plan and history are kept; useful observations and the final result go to memory.

## Data analysis

```bash
python examples/seed_demo_data.py   # copies demo_sales.csv into the workspace
```

Then create a task such as:

> Analyse demo/demo_sales.csv: rank branches by gross profit, show the monthly revenue trend, and save a short report.

A typical plan is `data_summary` (trend + branch ranking) → *write findings* (reasoning) → `report_generator`. The report appears in `data/workspace/reports/` and in the dashboard's **Reports** section. The demo data is synthetic (8 branches × 8 categories, January–June 2026), generated with [retail-analytics-toolkit](https://github.com/Anjub004/retail-analytics-toolkit) (MIT).

The data tools only read CSV files inside the workspace and accept structured parameters (group-by columns, metrics, filters). They never evaluate pandas `query`/`eval` strings. `data_summary` flags partial months and compares per-day averages, so a short month is not reported as a drop.

To let the agent only read and analyse data (no file writes, no internet), set `TOOL_PERMISSION_LEVEL=read`.

## Conversations

In **AI Workspace**, each message starts a task. Earlier messages in the conversation are given to the planner and the reviewer, so follow-ups like *"now the same for June"* work. The result is posted back as a reply.

```bash
curl -s -X POST http://127.0.0.1:8000/api/conversations -H "Content-Type: application/json" -d '{}'
curl -s -X POST http://127.0.0.1:8000/api/conversations/<id>/messages \
  -H "Content-Type: application/json" \
  -d '{"content": "Total revenue by branch for May in demo/demo_sales.csv"}'
curl -s -X POST http://127.0.0.1:8000/api/conversations/<id>/messages \
  -H "Content-Type: application/json" -d '{"content": "Now the same for June"}'
curl -s http://127.0.0.1:8000/api/conversations/<id>     # messages, including the replies
```

`CONVERSATION_CONTEXT_MESSAGES` controls how many earlier messages are used (default 10). Deleting a conversation removes its messages but keeps its tasks and their history.

## Long-term memory

Memory holds facts that are useful across tasks: final results, reviewer observations and your own notes, each with a category. Goals, plans and raw tool output stay in the execution log instead. The planner receives the most relevant entries for each new goal.

Use the **Memory** section, or:

```bash
curl -s -X POST http://127.0.0.1:8000/api/memory -H "Content-Type: application/json" \
  -d '{"content": "Gross profit in reports excludes VAT", "category": "finance"}'
curl -s "http://127.0.0.1:8000/api/memory?category=finance"
curl -s "http://127.0.0.1:8000/api/memory/search?q=gross+profit"
curl -s http://127.0.0.1:8000/api/memory/categories
curl -s -X PATCH http://127.0.0.1:8000/api/memory/<id> -H "Content-Type: application/json" \
  -d '{"category": "accounting"}'
curl -s -X DELETE http://127.0.0.1:8000/api/memory/<id>
```

## Clarification questions

When essential information is missing and a wrong guess would waste the task (which file? which month?), the planner asks **one** question instead of guessing. The task becomes `WAITING_INPUT`, and the dashboard shows the question with an answer box.

```bash
curl -s -X POST http://127.0.0.1:8000/api/tasks/<task_id>/answer \
  -H "Content-Type: application/json" -d '{"answer": "June 2026"}'
```

In a conversation, simply send the answer as the next message. The answer is added to the goal for every module (planner, executor, reviewer). A task can ask at most two questions; after that the planner must plan with stated assumptions.

## Human approval

An approval is requested when **any** of these is true:

- the tool says the specific call is sensitive (webhook notifications; overwriting an existing workspace file or report),
- the tool is listed in `APPROVAL_REQUIRED_TOOLS`,
- the planner marked the step `requires_approval`.

The agent records the action, reason, its own explanation and the exact tool input, sets `WAITING_APPROVAL`, and stops. Nothing runs until a human decides:

```bash
curl -s http://127.0.0.1:8000/api/approvals                       # pending
curl -s -X POST http://127.0.0.1:8000/api/approvals/<id>/approve
curl -s -X POST http://127.0.0.1:8000/api/approvals/<id>/reject \
  -H "Content-Type: application/json" -d '{"note": "Do not post externally"}'
```

On approval the stored input is executed as-is (never regenerated). On rejection the step is skipped (`APPROVAL_REJECTION_POLICY=skip`, default) or the task fails (`fail`). Each approval can be used once.

## Task reports

Every task can be downloaded as a Markdown report (goal, clarifications, result, plan, tool calls, approvals, token usage, timeline). Use **Download report** in the task drawer or the **Reports** section, or:

```bash
curl -s -OJ http://127.0.0.1:8000/api/tasks/<task_id>/report                # download
curl -s "http://127.0.0.1:8000/api/tasks/<task_id>/report?download=false"   # view
curl -s http://127.0.0.1:8000/api/reports                                   # reports the agent saved
curl -s http://127.0.0.1:8000/api/reports/<name>.md
```

Tool outputs in a task report are shortened to 800 characters.

## Monitoring

Monitoring mode (experimental) re-runs a goal on a schedule and notifies you only when something meaningful changed. Create a monitor in **Tasks → Monitors** or via the API:

```bash
curl -s -X POST http://127.0.0.1:8000/api/monitors \
  -H "Content-Type: application/json" \
  -d '{"name": "AI News Monitor", "goal": "Monitor meaningful developments in AI agents", "interval_minutes": 360}'
```

Every cycle the monitor: wakes up → runs the goal as a normal task → when it completes, compares the result with the previous one using the LLM → creates a dashboard notification **only** if there is a meaningful change → saves the result as the new baseline → sleeps until the next run. The first run only captures a baseline.

Other endpoints: `GET /api/monitors`, `POST /api/monitors/{id}/run`, `POST /api/monitors/{id}/enable|disable`, `DELETE /api/monitors/{id}`, `GET /api/notifications`.

Limitation: the scheduler is a single asyncio loop in the API process. Run one instance per database.

## Running without the server

The examples run the agent in-process:

```bash
python examples/simple_task.py        # calculator + file system
python examples/research_agent.py     # search/browse + summary + saved report
python examples/monitoring_agent.py --cycles 2 --pause 60
```

They need `OPENAI_API_KEY` and `OPENAI_MODEL` like the server does.
