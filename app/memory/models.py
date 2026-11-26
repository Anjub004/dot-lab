"""Pydantic models for the memory subsystem."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class MemoryKind(StrEnum):
    GOAL = "goal"
    PLAN = "plan"
    STEP = "step"
    TOOL_RESULT = "tool_result"
    OBSERVATION = "observation"
    RESULT = "result"
    NOTE = "note"


DEFAULT_CATEGORY = "general"
_CATEGORY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,49}$")


def normalize_category(value: str) -> str:
    """'Sales Reports ' -> 'sales_reports'; raises ValueError if still invalid."""
    cleaned = re.sub(r"\s+", "_", value.strip().lower())
    if not _CATEGORY.match(cleaned):
        raise ValueError("category must be 1-50 characters: lowercase letters, digits, '_' or '-'")
    return cleaned


class MemoryCreate(BaseModel):
    """Input for saving a memory."""

    kind: MemoryKind
    content: str = Field(min_length=1)
    category: str = DEFAULT_CATEGORY
    task_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("category")
    @classmethod
    def _check_category(cls, value: str) -> str:
        return normalize_category(value)


class CategoryCount(BaseModel):
    category: str
    count: int


class MemoryRecord(BaseModel):
    """A stored memory."""

    id: int
    kind: MemoryKind
    category: str
    content: str
    task_id: str | None
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime | None = None
    score: float | None = None
