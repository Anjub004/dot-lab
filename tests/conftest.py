"""Shared fixtures. The whole suite runs offline with a scripted fake LLM."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

# Isolate the module-level app in app.main from any developer .env / database.
_TMP = Path(tempfile.mkdtemp(prefix="dotlab-tests-"))
os.environ.update(
    {
        "OPENAI_API_KEY": "",
        "OPENAI_MODEL": "",
        "DATABASE_URL": f"sqlite:///{_TMP / 'import.db'}",
        "WORKSPACE_DIR": str(_TMP / "workspace"),
        "ENABLE_MONITOR_SCHEDULER": "false",
        "SEARCH_PROVIDER": "none",
        "SEARCH_API_KEY": "",
        "DOT_LAB_API_KEY": "",
        "NOTIFICATION_WEBHOOK_URL": "",
        "LOG_LEVEL": "WARNING",
    }
)

import pytest
from pydantic import BaseModel

from app.agent.evaluator import GoalEvaluation, StepEvaluation
from app.agent.executor import ToolArguments
from app.agent.planner import PlannedStep, PlannerOutput
from app.config import Settings
from app.context import AppContext, build_context

Response = BaseModel | Callable[[str], BaseModel] | Exception


class ScriptedLLM:
    """Fake LLM returning queued responses per output schema.

    The last queued response for a schema is reused once the queue is down to
    one item ("sticky"), which keeps scripts short for repeated evaluations.
    """

    def __init__(self, script: dict[type[BaseModel], list[Response]] | None = None) -> None:
        self.script: dict[type[BaseModel], list[Response]] = {
            k: list(v) for k, v in (script or {}).items()
        }
        self.calls: list[tuple[type[BaseModel], str]] = []

    @property
    def is_configured(self) -> bool:
        return True

    def add(self, schema: type[BaseModel], *responses: Response) -> None:
        self.script.setdefault(schema, []).extend(responses)

    async def generate(self, *, system: str, prompt: str, schema: type[Any]) -> Any:
        self.calls.append((schema, prompt))
        queue = self.script.get(schema)
        if not queue:
            raise AssertionError(f"ScriptedLLM has no response for {schema.__name__}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        if callable(item) and not isinstance(item, BaseModel):
            return item(prompt)
        return item

    def count(self, schema: type[BaseModel]) -> int:
        return sum(1 for s, _ in self.calls if s is schema)


# ---------------------------------------------------------------- builders --
def plan(*steps: tuple[str, str | None, bool]) -> PlannerOutput:
    return PlannerOutput(
        summary="test plan",
        steps=[PlannedStep(description=d, tool=t, requires_approval=a) for d, t, a in steps],
    )


def args(json_text: str) -> ToolArguments:
    return ToolArguments(arguments_json=json_text, explanation="test arguments")


def step_ok(next_action: str = "continue", observations: list[str] | None = None) -> StepEvaluation:
    return StepEvaluation(
        success=True,
        summary="step done",
        observations=observations or [],
        next_action=next_action,  # type: ignore[arg-type]
        reason="looks good",
    )


def goal_done(result: str = "Final answer") -> GoalEvaluation:
    return GoalEvaluation(
        goal_satisfied=True, final_result=result, reason="done", additional_steps=[]
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        workspace_dir=tmp_path / "workspace",
        enable_monitor_scheduler=False,
        log_level="WARNING",
        max_agent_iterations=30,
        task_timeout_seconds=30,
        tool_timeout_seconds=5,
    )


@pytest.fixture
def llm() -> ScriptedLLM:
    return ScriptedLLM()


@pytest.fixture
def ctx(settings: Settings, llm: ScriptedLLM) -> Iterator[AppContext]:
    context = build_context(settings, llm=llm, retry_base_delay=0)
    yield context
    context.db.dispose()
