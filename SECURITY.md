# Security

Dot Lab is an **experimental** agent. It is designed to be safe to run on your own machine for experimentation, not to be exposed to the internet or used with sensitive data. This document explains the protections that exist, and — just as importantly — their limits.

## Supported versions

| Version | Supported |
|---|---|
| 1.x | Yes |
| < 1.0 | No (pre-release milestones) |

## Reporting a vulnerability

Please do not open a public issue for security problems. Use GitHub's [private vulnerability reporting](https://github.com/Anjub004/dot-lab/security/advisories/new) ("Security" tab → "Report a vulnerability") with:

- what an attacker can do, and under which configuration,
- steps or a minimal goal / request to reproduce,
- the Dot Lab version (`GET /api/settings` or the dashboard's Settings page).

We aim to acknowledge reports within a week and to publish a fix and an advisory for confirmed issues. Please never include real API keys or personal data in a report.

**In scope:** escaping the workspace, reaching private networks through the browser, running code or shell commands, bypassing approvals or tool permission levels, leaking secrets through the API, logs or dashboard, and bypassing `DOT_LAB_API_KEY`.

**Out of scope:** the model giving wrong answers, prompt injection that stays within the documented limits below, and denial of service on a server you exposed publicly against this advice.

## Threat model (1.x)

- One trusted operator runs Dot Lab locally (or on a private network).
- The LLM and every web page it reads are **untrusted**: they may produce wrong or malicious instructions (prompt injection).
- The agent must not be able to affect anything outside its workspace, the configured search provider, public web pages (read-only) and an operator-configured webhook — and the webhook only with approval.

## Protections

### Secrets
- Read from environment variables / `.env` only; `.env` is git-ignored; `.env.example` holds placeholders.
- Stored as `pydantic.SecretStr`, never returned by the API (`/api/health` reports only whether AI is configured).
- A logging filter redacts configured secret values and common credential patterns (OpenAI-style keys, bearer tokens, `api_key=`/`token=`/`password=` pairs). `httpx`/`openai` loggers are kept at WARNING to avoid header/URL logging.

### API access
- Optional `DOT_LAB_API_KEY`: when set, all `/api/*` routes except `/api/health` require the `X-API-Key` header (constant-time comparison).
- Docker Compose publishes the port on `127.0.0.1` only.
- All request bodies are validated with Pydantic (lengths, ranges, enums). Unhandled errors return a generic message; details go to the log.

### File system tool
- Confined to `WORKSPACE_DIR` (default `data/workspace`).
- Rejects absolute paths (POSIX and Windows), `~`, drive letters, any `..` component, and null bytes; the final resolved path (after following symlinks) must be inside the workspace.
- Size limits on read and write; UTF-8 text only; writes are atomic.
- Overwriting an existing file requires human approval. There is no delete operation.

### Data and report tools
- `csv_analyzer` and `data_summary` read `.csv` files inside the workspace only (same path rules as the file system tool), up to `CSV_MAX_BYTES`, with pandas in a worker thread under the tool timeout.
- They take structured parameters (columns, metrics, filters with fixed operators). Column names are checked against the file; no pandas `query`/`eval` strings and no formulas are evaluated.
- `report_generator` writes Markdown only into `reports/` inside the workspace, with a validated file name; overwriting an existing report requires approval.
- Dashboard downloads of reports use a strict name pattern, so no other file can be served.

### Tool permission levels
- Every tool declares `read`, `write` or `external`; a tool that declares nothing is treated as `external`.
- `TOOL_PERMISSION_LEVEL` hides tools above the level from the planner **and** blocks them at execution time, so a model that names a hidden tool still cannot run it.

### Dashboard
- All server data is HTML-escaped before rendering; no inline event handlers built from data.
- The optional API key is kept in the browser's `localStorage` and sent as a header, never in URLs.

### Browser tool
- Read-only: no clicking, typing, form submission, downloads, cookies or logins.
- Only `http`/`https`; URLs with embedded credentials are rejected.
- Hosts that resolve to private, loopback, link-local (including cloud metadata `169.254.169.254`), reserved or multicast addresses are blocked unless `BROWSER_ALLOW_PRIVATE_NETWORKS=true`.
- Redirects are followed manually and each hop is re-checked. The Playwright backend checks every sub-request.
- Optional domain allow-list; response size and extracted text are capped; only text/HTML/JSON is read.

### Execution
- **No shell commands and no arbitrary code execution.** The calculator evaluates a whitelisted subset of Python's AST (numbers, arithmetic, a few math functions), with bounds on exponent and result size.
- Tool arguments are validated against each tool's Pydantic schema before execution.
- Limits: `MAX_AGENT_ITERATIONS`, `MAX_PLAN_STEPS`, `MAX_STEP_RETRIES`, `MAX_REPLANS`, `TOOL_TIMEOUT_SECONDS`, `TASK_TIMEOUT_SECONDS`, `MAX_CONCURRENT_TASKS`, `MAX_GOAL_LENGTH`.

### Human approval
- Required for webhook notifications (external communication), overwriting files, steps the planner marks sensitive, and tools in `APPROVAL_REQUIRED_TOOLS`.
- The human sees the exact tool input; on approval that exact input is executed (not regenerated). Approvals are single-use and are cancelled when a task is cancelled or fails.

## Known limitations

- **Prompt injection.** Web content can contain instructions aimed at the model. Prompts tell the model to treat page content as data, and the tool/approval design limits what an injected instruction can do, but this is not a complete defence. Keep approvals enabled and review results.
- **Hallucination.** The model can misread or invent facts. Results are not verified by a second source.
- **DNS rebinding / TOCTOU.** With the default `httpx` backend the hostname is resolved for the safety check and again by the HTTP client. A malicious DNS server could return different answers. Use `BROWSER_ALLOWED_DOMAINS` for stronger guarantees, or run Dot Lab in a network-isolated container.
- **Search provider.** Queries (which may include parts of your goal) are sent to the configured search provider.
- **OpenAI.** Goals, plans and tool results are sent to the OpenAI API as part of prompts. Do not use Dot Lab with data you are not allowed to share with that provider.
- **Single shared API key, no user accounts, no rate limiting, no CSRF protection.** Do not expose the server publicly.
- **Not durable.** Background tasks run in-process; a crash can interrupt a step, which is re-run after restart. Tool side effects are therefore at-least-once.
- **Approval scope.** Approval applies to a single tool call. Reasoning steps never need approval because they have no side effects.
- **Webhook URL** is trusted configuration; it is not subject to the browser's private-network block.
- **SQLite file permissions** are whatever your OS gives files in `data/`. Protect that directory: it contains task history and memory.
