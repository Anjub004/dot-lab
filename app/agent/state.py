"""Task/plan state models and the task status state machine.

These are plain Pydantic models with no database or LLM dependencies, so they
can be imported anywhere without creating circular imports.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)

ACTIVE_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.PENDING, TaskStatus.PLANNING, TaskStatus.RUNNING}
)

# Allowed status transitions. Anything not listed is rejected.
ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset(
        {
            TaskStatus.PLANNING,
            TaskStatus.RUNNING,
            TaskStatus.PAUSED,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
        }
    ),
    TaskStatus.PLANNING: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.PAUSED,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
            TaskStatus.PENDING,
        }
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.PLANNING,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.PAUSED,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.PENDING,
        }
    ),
    TaskStatus.WAITING_APPROVAL: frozenset(
        {TaskStatus.PENDING, TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    TaskStatus.PAUSED: frozenset({TaskStatus.PENDING, TaskStatus.CANCELLED}),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


def can_transition(current: TaskStatus, new: TaskStatus) -> bool:
    """Return True if ``current -> new`` is a legal status change."""
    return new in ALLOWED_TRANSITIONS[current]


class StepStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


FINISHED_STEP_STATUSES = frozenset({StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.SKIPPED})


class PlanStep(BaseModel):
    """One executable step of a plan."""

    id: str
    description: str
    tool: str | None = Field(
        default=None, description="Tool name, or None for a pure reasoning/writing step."
    )
    requires_approval: bool = False
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    result: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def finished(self) -> bool:
        return self.status in FINISHED_STEP_STATUSES


class Plan(BaseModel):
    """A structured execution plan for a goal."""

    goal: str
    summary: str = ""
    steps: list[PlanStep] = Field(default_factory=list)
    replans: int = 0

    def next_step(self) -> PlanStep | None:
        """First step that still needs work, in order."""
        for step in self.steps:
            if not step.finished:
                return step
        return None

    def get_step(self, step_id: str) -> PlanStep | None:
        return next((s for s in self.steps if s.id == step_id), None)

    def progress(self) -> tuple[int, int]:
        """(finished steps, total steps)."""
        done = sum(1 for s in self.steps if s.finished)
        return done, len(self.steps)

    def skip_remaining(self, reason: str) -> None:
        for step in self.steps:
            if not step.finished:
                step.status = StepStatus.SKIPPED
                step.error = reason

    def next_step_id(self) -> str:
        return f"step_{len(self.steps) + 1}"


class AgentState(BaseModel):
    """Snapshot of everything the agent loop needs to decide its next move."""

    task_id: str
    goal: str
    status: TaskStatus
    plan: Plan | None = None
    iterations: int = 0

    @property
    def needs_planning(self) -> bool:
        return self.plan is None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class EventType(StrEnum):
    TASK_CREATED = "TASK_CREATED"
    PLANNING_STARTED = "PLANNING_STARTED"
    PLAN_CREATED = "PLAN_CREATED"
    STEP_STARTED = "STEP_STARTED"
    TOOL_CALLED = "TOOL_CALLED"
    TOOL_FAILED = "TOOL_FAILED"
    STEP_COMPLETED = "STEP_COMPLETED"
    STEP_FAILED = "STEP_FAILED"
    STEP_RETRY = "STEP_RETRY"
    STEP_SKIPPED = "STEP_SKIPPED"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    REPLANNED = "REPLANNED"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"
    TASK_PAUSED = "TASK_PAUSED"
    TASK_RESUMED = "TASK_RESUMED"
    TASK_CANCELLED = "TASK_CANCELLED"
    MONITOR_RESULT = "MONITOR_RESULT"
