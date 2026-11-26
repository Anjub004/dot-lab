## What does this change?

<!-- A short description, and the issue it closes (e.g. "Closes #12"). -->

## Why?

## How was it tested?

<!-- Say honestly what you ran. "Not tested" is fine; a wrong answer is not. -->

- [ ] `ruff check . && ruff format --check .`
- [ ] `pytest` (offline suite)
- [ ] `pytest -m live` (only if prompts, structured outputs or the OpenAI client changed)
- [ ] `python scripts/ui_smoke_test.py` (only if the dashboard changed)

## Checklist

- [ ] Tests added or updated for the change
- [ ] Docs updated (`README.md`, `docs/`, `.env.example`) where behaviour or configuration changed
- [ ] `CHANGELOG.md` updated under **Unreleased** for user-visible changes
- [ ] New tools declare a permission level, validate input and say when they need approval
- [ ] Database changes include an Alembic migration and a migration test
- [ ] No secrets, personal data or `.env` files committed

## Security implications

<!-- Does this let the agent read, write or reach anything new? "None" is a valid answer. -->
