"""Planner tests."""

from __future__ import annotations

import pytest

from app.agent.planner import Planner, PlannerOutput, PlanValidationError, describe_tools
from app.agent.state import StepStatus
from app.tools.calculator import CalculatorTool
from app.tools.filesystem import FileSystemTool
from tests.conftest import ScriptedLLM, plan


@pytest.fixture
def tools(tmp_path):  # type: ignore[no-untyped-def]
    return [CalculatorTool(), FileSystemTool(tmp_path / "ws", max_bytes=10_000)]


async def test_planner_output(tools) -> None:  # type: ignore[no-untyped-def]
    llm = ScriptedLLM(
        {
            PlannerOutput: [
                plan(
                    ("Calculate totals", "calculator", False),
                    ("Analyse the totals", None, False),
                    ("Save the report", "filesystem", False),
                )
            ]
        }
    )
    result = await Planner(llm, max_steps=5).create_plan("Calculate and save", tools)

    assert result.goal == "Calculate and save"
    assert [s.id for s in result.steps] == ["step_1", "step_2", "step_3"]
    assert [s.tool for s in result.steps] == ["calculator", None, "filesystem"]
    assert all(s.status is StepStatus.PENDING for s in result.steps)
    # The structured plan round-trips through JSON (how it is persisted).
    assert type(result).model_validate_json(result.model_dump_json()) == result


async def test_planner_prompt_lists_tools_and_memory(tools) -> None:  # type: ignore[no-untyped-def]
    llm = ScriptedLLM({PlannerOutput: [plan(("x", None, False))]})
    await Planner(llm, max_steps=5).create_plan("goal", tools, memory_context="- earlier fact")
    _, prompt = llm.calls[0]
    assert "earlier fact" in prompt
    assert "calculator" in describe_tools(tools) and "filesystem" in describe_tools(tools)


async def test_planner_retries_on_unknown_tool(tools) -> None:  # type: ignore[no-untyped-def]
    llm = ScriptedLLM(
        {
            PlannerOutput: [
                plan(("Send email", "email", False)),
                plan(("Compute", "calculator", False)),
            ]
        }
    )
    result = await Planner(llm, max_steps=5).create_plan("goal", tools)
    assert result.steps[0].tool == "calculator"
    assert "unknown tool 'email'" in llm.calls[1][1]  # feedback was given to the model


async def test_planner_gives_up_after_invalid_plans(tools) -> None:  # type: ignore[no-untyped-def]
    llm = ScriptedLLM({PlannerOutput: [plan(*[(f"s{i}", None, False) for i in range(9)])]})
    with pytest.raises(PlanValidationError, match="maximum is 3"):
        await Planner(llm, max_steps=3).create_plan("goal", tools)


async def test_planner_rejects_empty_plan(tools) -> None:  # type: ignore[no-untyped-def]
    llm = ScriptedLLM({PlannerOutput: [PlannerOutput(summary="", steps=[])]})
    with pytest.raises(PlanValidationError, match="no steps"):
        await Planner(llm, max_steps=3).create_plan("goal", tools)
