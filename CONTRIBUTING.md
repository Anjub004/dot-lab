# Contributing to Dot Lab

Thanks for your interest! Dot Lab is meant to be a small, readable reference for agentic workflows, so contributions that keep it **simple, safe and well-tested** are the most welcome.

## Development setup

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # only needed to run real tasks; tests work without it
pytest
ruff check . && ruff format --check .
```

## Ground rules

- **Tests must run offline.** Mock the LLM (`tests/conftest.py::ScriptedLLM`) and HTTP (`httpx.MockTransport`). Never require an API key in tests.
- **No fake functionality.** If something needs an external service, add a clean interface plus a real implementation, and fail with an actionable message when it is not configured.
- **Security first.** New tools must validate input with a Pydantic model, respect timeouts, and declare when they need human approval. No shell execution, no arbitrary code execution, no unrestricted file or network access.
- Type hints, docstrings on public classes/functions, small functions, no dead code.
- Keep the frontend dependency-free (HTML/CSS/vanilla JS).

## Adding a tool

1. Create `app/tools/<name>.py` with an input model and a `BaseTool` subclass:

   ```python
   class MyInput(BaseModel):
       value: str = Field(max_length=200, description="...")


   class MyTool(BaseTool):
       name = "my_tool"
       description = "What it does, for the planner."
       input_model = MyInput

       def approval_reason(self, args: BaseModel) -> str | None:
           return None  # or a reason string for sensitive calls

       async def execute(self, args: BaseModel, context: ToolContext) -> ToolResult:
           params = self.typed(args, MyInput)
           return ToolResult(content="text for the LLM", data={"structured": "result"})
   ```

2. Raise `ToolError(message, retryable=...)` for expected failures and `ToolConfigurationError` when configuration is missing (and implement `availability()` so the planner hides the tool).
3. Register it in `build_default_registry` (`app/tools/__init__.py`).
4. Add tests in `tests/test_tools.py`, document configuration in `.env.example` and the README.

## Adding a search provider or memory backend

- Search: subclass `SearchProvider` in `app/tools/web_search.py` and add it to `build_search_provider`.
- Memory: implement `MemoryStore` (`save`, `retrieve`, `search`, `delete`) and pass it in `app/context.py`.

## Pull requests

- One focused change per PR, with tests.
- Describe the behaviour change and any security implications.
- Do not add branding or claims of affiliation with any company or product. Dot Lab is an independent project.

By contributing you agree that your contributions are licensed under the MIT License.
