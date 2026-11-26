# Contributing to Dot Lab

Thanks for your interest! Dot Lab is meant to be a small, readable reference for agentic workflows, so contributions that keep it **simple, safe and well-tested** are the most welcome. Please read the [Code of Conduct](CODE_OF_CONDUCT.md) first.

- [Ways to contribute](#ways-to-contribute)
- [Development setup](#development-setup)
- [Ground rules](#ground-rules)
- [Running the checks](#running-the-checks)
- [Adding a tool](#adding-a-tool)
- [Changing the database](#changing-the-database)
- [Changing the dashboard](#changing-the-dashboard)
- [Pull requests](#pull-requests)
- [Releases](#releases)

## Ways to contribute

- **Report a bug** or **request a feature** with the [issue templates](https://github.com/Anjub004/dot-lab/issues/new/choose).
- **Security problems**: please do not open a public issue; follow [SECURITY.md](SECURITY.md).
- **Improve the docs**: unclear explanations are bugs too.
- **Pick an issue** labelled `good first issue` or `help wanted`, and say in the issue that you are working on it.

For larger changes (new tools, new dependencies, changes to the agent loop or the database), please open an issue first so we can agree on the approach before you write code.

## Development setup

Requirements: Python 3.12 or newer, Git. Docker is optional.

```bash
git clone https://github.com/<your-username>/dot-lab.git
cd dot-lab
python3.12 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env     # only needed to run real tasks; tests work without it
pytest
```

Run the app with `uvicorn app.main:app --reload` and open <http://127.0.0.1:8000>. `python examples/seed_demo_data.py` adds demo sales data to the workspace.

## Ground rules

- **Tests must run offline.** Mock the LLM (`tests/conftest.py::ScriptedLLM`) and HTTP (`httpx.MockTransport`). Never require an API key in the default test run.
- **No fake functionality.** If something needs an external service, add a clean interface plus a real implementation, and fail with an actionable message when it is not configured.
- **Security first.** New tools validate input with a Pydantic model, declare a permission level, respect timeouts and say when they need human approval. No shell execution, no arbitrary code execution, no unrestricted file or network access.
- **No agent frameworks as required dependencies.** The point of Dot Lab is that the agent loop is readable plain Python.
- **No hard-coded model names.** The model always comes from `OPENAI_MODEL`.
- **Never commit secrets** (`.env`, keys, tokens) or personal data, including in tests, fixtures and screenshots.
- Type hints, docstrings on public classes and functions, small functions, no dead code.
- Keep the frontend dependency-free (HTML, CSS and vanilla JavaScript, no build step).
- Do not remove working features without discussing it in an issue first.

## Running the checks

CI runs the first two on every pull request (Python 3.12 and 3.13) and builds the Docker image.

```bash
ruff check . && ruff format --check .   # lint + formatting (ruff format . fixes formatting)
pytest                                  # offline suite, no API key needed
```

**Live tests** call the real OpenAI API with your key from `.env`. They are skipped by default and never run in CI; they make about 10–20 model calls. Run them when you change prompts, structured output models or `app/agent/llm.py`:

```bash
pytest -m live -v
```

**Dashboard browser test** (optional, not in CI): clicks through every section in a real Chromium with the fake LLM, checks the phone layout, and fails on any JavaScript error. Run it when you change `dashboard/`:

```bash
pip install playwright && playwright install chromium
python scripts/ui_smoke_test.py --screenshots ./screenshots
```

**README screenshots** are generated, not hand-made, so they stay honest: `python scripts/make_screenshots.py` (also needs `pillow`) runs the real tools on the demo data with scripted model wording.

## Adding a tool

1. Create `app/tools/<name>.py` with an input model and a `BaseTool` subclass:

   ```python
   class MyInput(BaseModel):
       value: str = Field(max_length=200, description="Shown to the model.")


   class MyTool(BaseTool):
       name = "my_tool"
       description = "What it does and when to use it, for the planner."
       input_model = MyInput
       permission = PermissionLevel.READ  # READ, WRITE or EXTERNAL (the default)
       timeout_seconds = 10.0  # optional; TOOL_TIMEOUT_SECONDS still caps it

       def approval_reason(self, args: BaseModel) -> str | None:
           return None  # or a reason string for sensitive calls

       async def execute(self, args: BaseModel, context: ToolContext) -> ToolResult:
           params = self.typed(args, MyInput)
           return ToolResult(content="text for the LLM", data={"structured": "result"})
   ```

2. Choose the permission level honestly:
   - `READ`: only reads the workspace or computes (no side effects),
   - `WRITE`: changes files in the workspace,
   - `EXTERNAL`: talks to anything outside the machine (internet, webhooks, other services).
3. Raise `ToolError(message, retryable=...)` for expected failures, `ToolInputError` for bad arguments the model can fix, and `ToolConfigurationError` when configuration is missing (and implement `availability()` so the planner hides the tool).
4. Run blocking work (pandas, file parsing) with `asyncio.to_thread` so it does not block other tasks.
5. Register it in `build_default_registry` (`app/tools/__init__.py`).
6. Add tests (see `tests/test_tools.py` and `tests/test_data_tools.py`), and document configuration in `.env.example` and [docs/configuration.md](docs/configuration.md).

Search providers subclass `SearchProvider` in `app/tools/web_search.py` (add them to `build_search_provider`). Memory backends implement `MemoryStore` in `app/memory/memory.py` and are wired in `app/context.py`.

## Changing the database

The schema is managed with [Alembic](https://alembic.sqlalchemy.org/). Migrations live in `app/database/migrations/versions/` and run automatically on start-up.

1. Change the models in `app/database/models.py`. **Name every constraint** (foreign keys, unique constraints), because SQLite cannot alter unnamed ones later.
2. Generate a migration against a database that is at the current head:

   ```bash
   DATABASE_URL=sqlite:///./data/dev.db alembic upgrade head
   DATABASE_URL=sqlite:///./data/dev.db alembic revision --autogenerate -m "short description"
   ```

3. Read the generated file and fix it by hand where needed: give new non-null columns a `server_default`, and keep both `upgrade()` and `downgrade()` working. `render_as_batch` is on, so column changes rebuild the table; `Database.migrate()` turns SQLite foreign keys off during that rebuild so existing rows are not cascade-deleted.
4. Add or extend a test in `tests/test_migrations.py`. Existing tests check that a fresh database matches the models, that upgrades keep data and that downgrade → upgrade works.

## Changing the dashboard

- Everything is in `dashboard/` (`index.html`, `styles.css`, `app.js`). There is no build step; reload the page.
- Escape all server data with `esc()` before putting it into HTML. Use `data-action` attributes for buttons, not inline `onclick`.
- Check the layout at phone width (390 px) as well as desktop, and run `scripts/ui_smoke_test.py`.

## Pull requests

1. Fork the repository and create a branch from `main` (`fix/…`, `feat/…`, `docs/…`).
2. Keep one focused change per PR, with tests. Update docs and `CHANGELOG.md` (under **Unreleased**) for user-visible changes.
3. Make sure `ruff` and `pytest` pass locally.
4. Fill in the pull request template: what changed, why, how you tested it, and any security implications. Please say honestly which checks you ran; "not tested" is a fine answer, a wrong one is not.
5. A maintainer reviews it. Be patient and kind; this is a volunteer project.

Commit messages: a short imperative summary line (`Add Excel reader tool`), a blank line, then the why.

Do not add branding or claims of affiliation with any company or product. Dot Lab is an independent project.

## Releases

Dot Lab follows [Semantic Versioning](https://semver.org/). Maintainers release by moving the **Unreleased** notes in `CHANGELOG.md` to a new version, updating the version in `pyproject.toml` and `app/__init__.py`, and publishing a GitHub release with a `vX.Y.Z` tag.

## License

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE).
