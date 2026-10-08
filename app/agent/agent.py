"""The Dot Lab agent loop.

``Agent.run(task_id)`` drives one task forward until it completes, fails,
needs human approval, is paused/cancelled, or hits a safety limit::

    while task is active:
        reload state from the database        (sees pause/cancel immediately)
        enforce the iteration limit
        if there is no plan: create one
        pick the next unfinished step
        if none left: evaluate the goal -> finish, or add steps (bounded re-plan)
        resolve tool arguments; check approval policy
        if approval is required: record request, set WAITING_APPROVAL, exit
        execute the tool (timeout + retries) or a reasoning step
        record tool call, events and memory
        evaluate the step -> continue / retry / finish / fail

All state lives in the database, so a task can be resumed after an approval,
a pause, or a process restart. The whole run is wrapped in a timeout.
"""

from __future__ import annotations

import asyncio
import logging
from enum import StrEnum

from pydantic import BaseModel

from app.agent.evaluator import Evaluator, StepEvaluation
from app.agent.executor import StepExecutor
from app.agent.llm import LLMClient, LLMConfigurationError, LLMError
from app.agent.planner import Planner, PlannerOutput
from app.agent.state import (
    EventType,
    Plan,
    PlanStep,
    StepStatus,
    TaskStatus,
)
from app.approvals.manager import ApprovalManager, ApprovalStatus
from app.config import Settings
from app.database.database import DatabaseError, utcnow
from app.logging_config import redact
from app.memory.memory import MemoryStore
from app.memory.models import MemoryCreate, MemoryKind
from app.services.task_service import TaskService, TaskView
from app.tools.base import BaseTool, ToolContext, ToolError, ToolRegistry

logger = logging.getLogger(__name__)

STEP_RESULT_CHARS = 6000
CONTEXT_STEP_CHARS = 3000
CONTEXT_TOTAL_CHARS = 24_000
NON_TERMINAL = {
    TaskStatus.PENDING,
    TaskStatus.PLANNING,
    TaskStatus.RUNNING,
    TaskStatus.WAITING_APPROVAL,
    TaskStatus.PAUSED,
}


class StepOutcome(StrEnum):
    CONTINUE = "continue"
    STOP = "stop"


class _Skip:
    """Sentinel: the step was skipped (e.g. approval rejected)."""


class Agent:
    """Goal-driven agent: plan → act → observe → evaluate, with human approval gates."""

    def __init__(
        self,
        *,
        llm: LLMClient,
        tools: ToolRegistry,
        memory: MemoryStore,
        approvals: ApprovalManager,
        tasks: TaskService,
        settings: Settings,
        retry_base_delay: float = 1.0,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.memory = memory
        self.approvals = approvals
        self.tasks = tasks
        self.settings = settings
        self.planner = Planner(llm, max_steps=settings.max_plan_steps)
        self.executor = StepExecutor(
            llm,
            tool_timeout_seconds=settings.tool_timeout_seconds,
            max_tool_retries=settings.max_step_retries,
            retry_base_delay=retry_base_delay,
        )
        self.evaluator = Evaluator(llm)

    # ------------------------------------------------------------------ run --
    async def run(self, task_id: str) -> TaskStatus:
        """Advance a task as far as possible. Safe to call repeatedly (resume)."""
        task = self.tasks.get_task(task_id)
        if task.status not in (TaskStatus.PENDING, TaskStatus.PLANNING, TaskStatus.RUNNING):
            return task.status
        start = TaskStatus.RUNNING if task.plan else TaskStatus.PLANNING
        allowed = {TaskStatus.PENDING, TaskStatus.PLANNING, TaskStatus.RUNNING}
        if not self.tasks.try_transition(task_id, allowed, start, error=None):
            return self.tasks.get_task(task_id).status

        try:
            await asyncio.wait_for(self._loop(task_id), timeout=self.settings.task_timeout_seconds)
        except TimeoutError:
            self._fail(task_id, f"Task timed out after {self.settings.task_timeout_seconds}s")
        except asyncio.CancelledError:
            raise
        except LLMConfigurationError as exc:
            self._fail(task_id, str(exc))
        except LLMError as exc:
            self._fail(task_id, f"AI model error: {exc}")
        except DatabaseError as exc:
            logger.exception("database error", extra={"task_id": task_id})
            self._fail(task_id, str(exc))
        except Exception as exc:
            logger.exception("unexpected agent error", extra={"task_id": task_id})
            self._fail(task_id, f"Unexpected error: {type(exc).__name__}: {str(exc)[:300]}")
        return self.tasks.get_task(task_id).status

    async def _loop(self, task_id: str) -> None:
        while True:
            task = self.tasks.get_task(task_id)
            if task.status not in (TaskStatus.PLANNING, TaskStatus.RUNNING):
                return  # paused, cancelled, waiting approval or finished
            if task.iterations >= self.settings.max_agent_iterations:
                self._fail(
                    task_id,
                    f"Stopped after reaching MAX_AGENT_ITERATIONS "
                    f"({self.settings.max_agent_iterations}).",
                )
                return
            self.tasks.increment_iterations(task_id)

            if task.plan is None:
                await self._create_plan(task)
                continue

            step = task.plan.next_step()
            if step is None:
                if await self._finalize(task, task.plan):
                    return
                continue

            if await self._execute_step(task, task.plan, step) is StepOutcome.STOP:
                return

    # ------------------------------------------------------------- planning --
    async def _create_plan(self, task: TaskView) -> None:
        self.tasks.add_event(task.id, EventType.PLANNING_STARTED, "Creating plan")
        self._remember(task.id, MemoryKind.GOAL, task.goal)
        plan = await self.planner.create_plan(
            task.goal, self.tools.available(), self._memory_context(task.goal, task.id)
        )
        self.tasks.save_plan(task.id, plan)
        steps = "\n".join(f"{s.id}: [{s.tool or 'reasoning'}] {s.description}" for s in plan.steps)
        self.tasks.add_event(
            task.id,
            EventType.PLAN_CREATED,
            f"Plan with {len(plan.steps)} steps: {plan.summary}",
            data={"steps": [s.model_dump(mode="json") for s in plan.steps]},
        )
        self._remember(task.id, MemoryKind.PLAN, f"{plan.summary}\n{steps}")
        self.tasks.try_transition(task.id, {TaskStatus.PLANNING}, TaskStatus.RUNNING)

    # ------------------------------------------------------------ execution --
    async def _execute_step(self, task: TaskView, plan: Plan, step: PlanStep) -> StepOutcome:
        tool: BaseTool | None = None
        if step.tool:
            try:
                tool = self.tools.get(step.tool)
            except ToolError as exc:
                return self._finish_step(task, plan, step, False, str(exc), None)

        step.status = StepStatus.RUNNING
        step.started_at = step.started_at or utcnow()
        self.tasks.save_plan(task.id, plan, current_step_id=step.id)
        self.tasks.add_event(task.id, EventType.STEP_STARTED, step.description, step_id=step.id)
        context = self._step_context(plan)

        if tool is None:
            output = await self.executor.reason(task.goal, step, context)
            succeeded, outcome = True, output
        else:
            resolved = await self._resolve_arguments(task, plan, step, tool, context)
            if resolved is None:
                return StepOutcome.STOP
            if isinstance(resolved, _Skip):
                return StepOutcome.CONTINUE
            succeeded, outcome = await self._run_tool(task, step, tool, resolved)

        step.attempts += 1
        evaluation = await self.evaluator.evaluate_step(task.goal, step, outcome, succeeded)
        for observation in evaluation.observations[:5]:
            self._remember(task.id, MemoryKind.OBSERVATION, observation, step_id=step.id)
        return self._finish_step(task, plan, step, succeeded, outcome, evaluation)

    async def _run_tool(
        self, task: TaskView, step: PlanStep, tool: BaseTool, args: BaseModel
    ) -> tuple[bool, str]:
        run = await self.executor.run_tool(
            tool, args, ToolContext(task_id=task.id, step_id=step.id)
        )
        output = run.result.content if run.result else None
        self.tasks.record_tool_call(
            task_id=task.id,
            step_id=step.id,
            tool=tool.name,
            tool_input=run.args,
            output=output,
            status="SUCCESS" if run.success else "ERROR",
            error=run.error,
            attempt=run.attempts,
            duration_ms=run.duration_ms,
        )
        if run.success and output is not None:
            self.tasks.add_event(
                task.id, EventType.TOOL_CALLED, f"{tool.name} succeeded", step_id=step.id
            )
            self._remember(
                task.id, MemoryKind.TOOL_RESULT, f"[{tool.name}] {output}", step_id=step.id
            )
            return True, output
        self.tasks.add_event(
            task.id, EventType.TOOL_FAILED, f"{tool.name} failed: {run.error}", step_id=step.id
        )
        return False, f"ERROR: {run.error}"

    async def _resolve_arguments(
        self, task: TaskView, plan: Plan, step: PlanStep, tool: BaseTool, context: str
    ) -> BaseModel | _Skip | None:
        """Return validated tool args, a _Skip, or None if the loop must stop."""
        approval = self.approvals.active_for_step(task.id, step.id)
        if approval is not None:
            if approval.status is ApprovalStatus.PENDING:
                self._wait_for_approval(task, plan, step)
                return None
            self.approvals.consume(approval.id)
            if approval.status is ApprovalStatus.APPROVED:
                self.tasks.add_event(
                    task.id,
                    EventType.APPROVAL_GRANTED,
                    f"Approved: {approval.action}",
                    step_id=step.id,
                )
                return tool.parse_input(approval.input)
            return self._handle_rejection(task, plan, step, approval.decision_note)

        args, explanation = await self.executor.prepare_arguments(task.goal, step, tool, context)
        reason = self.approvals.policy_reason(
            tool_name=tool.name,
            step_requires_approval=step.requires_approval,
            tool_reason=tool.approval_reason(args),
        )
        if reason is None:
            return args

        tool_input = args.model_dump(mode="json")
        self.approvals.request(
            task_id=task.id,
            step_id=step.id,
            tool=tool.name,
            action=f"{tool.name}: {step.description}",
            reason=reason,
            explanation=explanation,
            tool_input=tool_input,
        )
        self.tasks.add_event(
            task.id,
            EventType.APPROVAL_REQUESTED,
            f"Waiting for approval: {reason}",
            step_id=step.id,
            data={"tool": tool.name, "input": tool_input},
        )
        self._wait_for_approval(task, plan, step)
        return None

    def _wait_for_approval(self, task: TaskView, plan: Plan, step: PlanStep) -> None:
        step.status = StepStatus.WAITING_APPROVAL
        self.tasks.save_plan(task.id, plan, current_step_id=step.id)
        self.tasks.try_transition(task.id, {TaskStatus.RUNNING}, TaskStatus.WAITING_APPROVAL)

    def _handle_rejection(
        self, task: TaskView, plan: Plan, step: PlanStep, note: str | None
    ) -> _Skip | None:
        message = "Rejected by human reviewer" + (f": {note}" if note else "")
        self.tasks.add_event(task.id, EventType.APPROVAL_REJECTED, message, step_id=step.id)
        if self.settings.approval_rejection_policy == "fail":
            step.status = StepStatus.FAILED
            step.error = message
            self.tasks.save_plan(task.id, plan, current_step_id=step.id)
            self._fail(task.id, f"Step '{step.description}' was rejected by a human reviewer.")
            return None
        step.status = StepStatus.SKIPPED
        step.error = message
        step.finished_at = utcnow()
        self.tasks.save_plan(task.id, plan, current_step_id=step.id)
        self._remember(
            task.id,
            MemoryKind.OBSERVATION,
            f"Human rejected action '{step.description}'. {note or ''}".strip(),
            step_id=step.id,
        )
        return _Skip()

    def _finish_step(
        self,
        task: TaskView,
        plan: Plan,
        step: PlanStep,
        succeeded: bool,
        outcome: str,
        evaluation: StepEvaluation | None,
    ) -> StepOutcome:
        step.result = outcome[:STEP_RESULT_CHARS]
        step.error = None if succeeded else outcome[:1000]
        action = evaluation.next_action if evaluation else "continue"
        reason = evaluation.reason if evaluation else outcome

        if action == "retry" and step.attempts <= self.settings.max_step_retries:
            step.status = StepStatus.PENDING
            self.tasks.save_plan(task.id, plan, current_step_id=step.id)
            self.tasks.add_event(
                task.id, EventType.STEP_RETRY, f"Retrying: {reason}", step_id=step.id
            )
            return StepOutcome.CONTINUE

        step.finished_at = utcnow()
        if action == "fail":
            step.status = StepStatus.FAILED
            self.tasks.save_plan(task.id, plan, current_step_id=step.id)
            self._fail(task.id, f"Agent stopped at {step.id}: {reason}")
            return StepOutcome.STOP

        step.status = StepStatus.COMPLETED if succeeded else StepStatus.FAILED
        event = EventType.STEP_COMPLETED if succeeded else EventType.STEP_FAILED
        summary = evaluation.summary if evaluation else outcome
        self.tasks.add_event(task.id, event, summary[:1000], step_id=step.id)
        if action == "finish":
            plan.skip_remaining("Skipped: goal already achieved")
        self.tasks.save_plan(task.id, plan, current_step_id=step.id)
        return StepOutcome.CONTINUE

    # ----------------------------------------------------------- finishing --
    async def _finalize(self, task: TaskView, plan: Plan) -> bool:
        """Evaluate the goal. Returns True when the task reached a final state."""
        available = [t.name for t in self.tools.available()]
        evaluation = await self.evaluator.evaluate_goal(
            plan, self._step_context(plan, total=CONTEXT_TOTAL_CHARS * 2), available
        )

        if (
            not evaluation.goal_satisfied
            and evaluation.additional_steps
            and plan.replans < self.settings.max_replans
        ):
            extra = evaluation.additional_steps[: self.settings.max_plan_steps]
            problems = self.planner.validate(PlannerOutput(summary="", steps=extra), set(available))
            if not problems:
                plan.replans += 1
                added = self.planner.extend_plan(plan, extra)
                self.tasks.save_plan(task.id, plan)
                self.tasks.add_event(
                    task.id,
                    EventType.REPLANNED,
                    f"Goal not yet satisfied ({evaluation.reason}); added {len(added)} steps",
                    data={"steps": [s.model_dump(mode="json") for s in added]},
                )
                return False

        result = evaluation.final_result
        any_success = any(s.status is StepStatus.COMPLETED for s in plan.steps)
        if not evaluation.goal_satisfied:
            result += f"\n\n> Note: the agent could not fully satisfy the goal: {evaluation.reason}"
        if not evaluation.goal_satisfied and not any_success:
            self._fail(task.id, f"Goal not achieved: {evaluation.reason}", result=result)
            return True

        if self.tasks.try_transition(
            task.id, {TaskStatus.RUNNING}, TaskStatus.COMPLETED, result=result, current_step_id=None
        ):
            self.tasks.add_event(task.id, EventType.TASK_COMPLETED, "Task completed")
            self._remember(task.id, MemoryKind.RESULT, f"Goal: {task.goal}\n\n{result}")
        return True

    def _fail(self, task_id: str, message: str, result: str | None = None) -> None:
        safe = redact(message, self.settings.secret_values())[:2000]
        fields: dict[str, object] = {"error": safe}
        if result is not None:
            fields["result"] = result
        if self.tasks.try_transition(task_id, NON_TERMINAL, TaskStatus.FAILED, **fields):
            self.approvals.cancel_for_task(task_id)
            self.tasks.add_event(task_id, EventType.TASK_FAILED, safe)
            logger.warning("task failed", extra={"task_id": task_id, "error": safe})

    # -------------------------------------------------------------- context --
    def _step_context(self, plan: Plan, total: int = CONTEXT_TOTAL_CHARS) -> str:
        """Results of finished steps, newest kept when over budget."""
        blocks = []
        for s in plan.steps:
            if s.status in (StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.SKIPPED):
                body = (s.result or s.error or "")[:CONTEXT_STEP_CHARS]
                blocks.append(
                    f"### {s.id} [{s.tool or 'reasoning'} | {s.status}] {s.description}\n{body}"
                )
        text = "\n\n".join(blocks)
        return text[-total:] if len(text) > total else text

    def _memory_context(self, goal: str, task_id: str) -> str:
        records = self.memory.search(
            goal,
            limit=5,
            kinds=[MemoryKind.RESULT, MemoryKind.OBSERVATION],
            exclude_task_id=task_id,
        )
        return "\n".join(
            f"- ({r.kind}, {r.created_at:%Y-%m-%d}) {r.content[:500]}" for r in records
        )

    def _remember(
        self, task_id: str, kind: MemoryKind, content: str, step_id: str | None = None
    ) -> None:
        if not content.strip():
            return
        metadata = {"step_id": step_id} if step_id else {}
        self.memory.save(
            MemoryCreate(kind=kind, content=content, task_id=task_id, metadata=metadata)
        )
