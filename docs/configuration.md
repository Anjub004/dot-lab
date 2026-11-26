# Configuration

Dot Lab reads its settings from environment variables, or from a `.env` file in the project root (copy [`.env.example`](../.env.example)). Settings are validated on start-up; an invalid value stops the server with a clear error. **Never commit your `.env`.**

The dashboard's **Settings** section shows the active configuration (read-only, without secrets).

## Minimum

```dotenv
OPENAI_API_KEY=<your OpenAI API key>
OPENAI_MODEL=<a model that supports Structured Outputs on the Responses API>
```

Dot Lab does not hard-code a model name. Choose one from the [OpenAI models page](https://platform.openai.com/docs/models). On start-up, and in `GET /api/health`, Dot Lab checks that the model is available to your key. Without a key and model the server still starts, but creating tasks returns `503` and the dashboard shows "AI not configured".

## OpenAI

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | – | Required to run tasks. Stored as a secret and redacted from logs. |
| `OPENAI_MODEL` | – | Required. Any model with Structured Outputs on the Responses API. |
| `OPENAI_TIMEOUT_SECONDS` | `60` | Timeout per API call. |
| `OPENAI_MAX_RETRIES` | `2` | SDK retries for transient errors. |

## Storage

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./data/dot_lab.db` | SQLAlchemy URL (relative to the project root). Migrations run automatically on start-up. |
| `WORKSPACE_DIR` | `./data/workspace` | The **only** folder the file-system, data and report tools can touch. |
| `MAX_FILE_BYTES` | `1000000` | Largest file the file-system tool reads or writes, and largest report. |
| `CSV_MAX_BYTES` | `20000000` | Largest CSV the data tools will read (20 MB). |

## Tools

| Variable | Default | Purpose |
|---|---|---|
| `TOOL_PERMISSION_LEVEL` | `external` | Highest tool permission the agent may use. `read`: calculator, `csv_analyzer`, `data_summary`. `write`: + file system, `report_generator`. `external`: + web search, browser, notification. Tools above the level are hidden from the planner and blocked if called. |
| `SEARCH_PROVIDER` | `none` | `none`, `tavily` or `brave`. With `none`, web search is hidden from the planner. |
| `SEARCH_API_KEY` | – | Key for the search provider. |
| `SEARCH_MAX_RESULTS` | `5` | Results per search (1–10). |
| `BROWSER_BACKEND` | `httpx` | `httpx` (no JavaScript) or `playwright` (`pip install playwright && playwright install chromium`). |
| `BROWSER_ALLOWED_DOMAINS` | empty | Comma-separated allow-list, e.g. `arxiv.org,github.com`. Empty = any public site. |
| `BROWSER_ALLOW_PRIVATE_NETWORKS` | `false` | Keep `false` unless you understand the SSRF risk. |
| `BROWSER_MAX_CHARS` | `20000` | Text returned per page. |
| `BROWSER_MAX_BYTES` | `2000000` | Largest response downloaded per page. |
| `NOTIFICATION_WEBHOOK_URL` | – | Optional external webhook. Every webhook message needs approval. |

## Agent limits and approvals

| Variable | Default | Purpose |
|---|---|---|
| `MAX_AGENT_ITERATIONS` | `30` | Loop iterations per task. |
| `MAX_PLAN_STEPS` | `8` | Steps per plan. |
| `MAX_STEP_RETRIES` | `2` | Retries per step. |
| `MAX_REPLANS` | `1` | Times the reviewer may add steps. |
| `TOOL_TIMEOUT_SECONDS` | `30` | Upper limit per tool call. A tool can set a shorter one; the shorter wins. |
| `TASK_TIMEOUT_SECONDS` | `900` | Per run segment (time waiting for approval or input is not counted). |
| `MAX_CONCURRENT_TASKS` | `2` | Tasks running at the same time. |
| `MAX_GOAL_LENGTH` | `2000` | Characters per goal. |
| `APPROVAL_REQUIRED_TOOLS` | empty | Comma-separated tools that always need approval, e.g. `filesystem,browser`. |
| `APPROVAL_REJECTION_POLICY` | `skip` | On rejection: `skip` the step or `fail` the task. |
| `CONVERSATION_CONTEXT_MESSAGES` | `10` | Earlier conversation messages given to the planner and reviewer. `0` = off. |

## Budgets

Every LLM call's token usage is recorded per task. A task fails cleanly once it reaches a limit. The check happens before each call, so a task can exceed its limit by at most one call.

| Variable | Default | Purpose |
|---|---|---|
| `MAX_TASK_TOKENS` | `200000` | Input + output tokens per task, across all its runs. `0` = no limit. |
| `MAX_TASK_COST_USD` | – | Optional cost limit. Needs both prices below. |
| `OPENAI_INPUT_COST_PER_1M`, `OPENAI_OUTPUT_COST_PER_1M` | – | Your model's prices in USD per 1M tokens, copied from the [OpenAI pricing page](https://openai.com/api/pricing/). Used only to **estimate** cost; it is not your bill. |

## Monitoring

| Variable | Default | Purpose |
|---|---|---|
| `ENABLE_MONITOR_SCHEDULER` | `true` | Run scheduled monitors in the API process. |
| `MONITOR_POLL_SECONDS` | `30` | How often the scheduler checks for due monitors. |
| `MONITOR_MIN_INTERVAL_MINUTES` | `5` | Shortest allowed monitor interval. |

## Server

| Variable | Default | Purpose |
|---|---|---|
| `DOT_LAB_API_KEY` | – | If set, every `/api` request except `/api/health` needs the header `X-API-Key`. Enter it once in the dashboard's **Settings** (stored only in that browser). Strongly recommended if anyone but you can reach the server. |
| `LOG_LEVEL` | `INFO` | Python log level. |
| `LOG_JSON` | `true` | Structured JSON logs (secrets are redacted). |

## Recommended profiles

**Local data analysis only** (no internet, no file writes):

```dotenv
TOOL_PERMISSION_LEVEL=read
```

**Shared machine or server**:

```dotenv
DOT_LAB_API_KEY=<a long random string>
MAX_TASK_TOKENS=100000
APPROVAL_REQUIRED_TOOLS=filesystem,report_generator
```

Docker Compose binds to `127.0.0.1` by default. Put a reverse proxy with TLS in front before exposing it to a network; see [SECURITY.md](../SECURITY.md).
