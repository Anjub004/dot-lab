"""Pydantic models for the memory subsystem."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class MemoryKind(StrEnum):
    GOAL = "goal"
    PLAN = "plan"
    STEP = "step"
    TOOL_RESULT = "tool_result"
    OBSERVATION = "observation"
    RESULT = "result"
    NOTE = "note"


class MemoryCreate(BaseModel):
    """Input for saving a memory."""

    kind: MemoryKind
    content: str = Field(min_length=1)
    task_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryRecord(BaseModel):
    """A stored memory."""

    id: int
    kind: MemoryKind
    content: str
    task_id: str | None
    metadata: dict[str, Any]
    created_at: datetime
    score: float | None = None
