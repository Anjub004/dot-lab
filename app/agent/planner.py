"""Planner: turns a high-level goal into a structured, validated plan."""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, Field

from app.agent.llm import LLMClient, LLMError
from app.agent.state import Plan, PlanStep
from app.tools.base import BaseTool

logger = logging.getLogger(__name__)

# --- LLM output schemas ------------------------------------------------------
# These models are sent to OpenAI Structured Outputs, so they intentionally
# avoid defaults, dicts and validation keywords that strict mode rejects.


class PlannedStep(BaseModel):
    description: str = Field(description="What this step does, specific and actionable.")
    tool: str | None = Field(
        description="Exact tool name to use, or null for a reasoning/writing step."
    )
    requires_approval: bool = Field(
        description="True if the step communicates externally, deletes, or overwrites data."
    )


class PlannerOutput(BaseModel):
    summary: str = Field(description="One-sentence summary of the approach.")
    steps: list[PlannedStep]


PLANNER_SYSTEM = """You are the planning module of Dot Lab, an experimental autonomous agent.
Break the user's goal into a short, ordered list of concrete steps.

Rules:
- Use only the tools listed below, referenced by their exact name. Use tool = null for
  steps that only need reasoning or writing (analysing, comparing, summarising).
- Each tool step must be achievable with ONE call to that tool.
- Prefer the fewest steps that fully achieve the goal (maximum {max_steps}).
- The last reasoning step should produce the final deliverable for the user.
- Only save files or send notifications if the goal asks for it.
- Set requires_approval = true for steps that contact external parties, overwrite or
  delete data, or are otherwise risky.
- If a needed tool is not listed, plan around it using the tools that ARE listed.

Available tools:
{tools}
"""


def describe_tools(tools: list[BaseTool]) -> str:
    """Compact tool catalogue for prompts."""
    if not tools:
        return "(no tools available — use reasoning steps only)"
    blocks = []
    for tool in tools:
        schema = json.dumps(tool.input_schema.get("properties", {}), separators=(",", ":"))
        blocks.append(f"- {tool.name}: {tool.description}\n  input properties: {schema}")
    return "\n".join(blocks)


class PlanValidationError(LLMError):
    pass


class Planner:
    """Creates plans with the LLM and validates them against the tool registry."""

    def __init__(self, llm: LLMClient, max_steps: int, max_attempts: int = 2) -> None:
        self.llm = llm
        self.max_steps = max_steps
        self.max_attempts = max_attempts

    async def create_plan(self, goal: str, tools: list[BaseTool], memory_context: str = "") -> Plan:
        """Ask the LLM for a plan, re-asking with feedback if it is invalid."""
        system = PLANNER_SYSTEM.format(max_steps=self.max_steps, tools=describe_tools(tools))
        prompt = f"Goal:\n{goal}\n"
        if memory_context:
            prompt += f"\nPossibly relevant memory from earlier tasks:\n{memory_context}\n"

        feedback = ""
        for attempt in range(1, self.max_attempts + 1):
            output = await self.llm.generate(
                system=system, prompt=prompt + feedback, schema=PlannerOutput
            )
            problems = self.validate(output, {t.name for t in tools})
            if not problems:
                return self.to_plan(goal, output)
            logger.warning("invalid plan (attempt %s): %s", attempt, "; ".join(problems))
            feedback = "\n\nYour previous plan was invalid:\n- " + "\n- ".join(problems)
        raise PlanValidationError("Planner could not produce a valid plan: " + "; ".join(problems))

    def validate(self, output: PlannerOutput, tool_names: set[str]) -> list[str]:
        problems: list[str] = []
        if not output.steps:
            problems.append("The plan has no steps.")
        if len(output.steps) > self.max_steps:
            problems.append(f"The plan has {len(output.steps)} steps; maximum is {self.max_steps}.")
        for i, step in enumerate(output.steps, 1):
            if not step.description.strip():
                problems.append(f"Step {i} has an empty description.")
            if step.tool is not None and step.tool not in tool_names:
                problems.append(
                    f"Step {i} uses unknown tool '{step.tool}'. Valid tools: "
                    f"{', '.join(sorted(tool_names)) or 'none'} (or null)."
                )
        return problems

    @staticmethod
    def to_plan(goal: str, output: PlannerOutput) -> Plan:
        return Plan(
            goal=goal,
            summary=output.summary,
            steps=[
                PlanStep(
                    id=f"step_{i}",
                    description=s.description.strip(),
                    tool=s.tool,
                    requires_approval=s.requires_approval,
                )
                for i, s in enumerate(output.steps, 1)
            ],
        )

    @staticmethod
    def extend_plan(plan: Plan, steps: list[PlannedStep]) -> list[PlanStep]:
        """Append additional steps (from re-planning) and return them."""
        added: list[PlanStep] = []
        for s in steps:
            step = PlanStep(
                id=plan.next_step_id(),
                description=s.description.strip(),
                tool=s.tool,
                requires_approval=s.requires_approval,
            )
            plan.steps.append(step)
            added.append(step)
        return added
