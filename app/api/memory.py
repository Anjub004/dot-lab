"""Long-term memory endpoints: list, search, create notes, update, delete, categories."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator

from app.api.deps import get_ctx
from app.context import AppContext
from app.memory.models import (
    DEFAULT_CATEGORY,
    CategoryCount,
    MemoryCreate,
    MemoryKind,
    MemoryRecord,
    normalize_category,
)

router = APIRouter(prefix="/memory", tags=["memory"])


class NoteCreate(BaseModel):
    """A fact the user wants Dot Lab to remember (stored as kind 'note')."""

    content: str = Field(min_length=1, max_length=20_000)
    category: str = DEFAULT_CATEGORY

    @field_validator("content")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("category")
    @classmethod
    def _category(cls, value: str) -> str:
        return normalize_category(value)


class MemoryUpdate(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=20_000)
    category: str | None = None

    @field_validator("category")
    @classmethod
    def _category(cls, value: str | None) -> str | None:
        return normalize_category(value) if value is not None else None

    @model_validator(mode="after")
    def _something_to_change(self) -> MemoryUpdate:
        if self.content is None and self.category is None:
            raise ValueError("provide content and/or category")
        if self.content is not None and not self.content.strip():
            raise ValueError("content must not be empty")
        return self


def _category_query(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return normalize_category(value)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.get("", response_model=list[MemoryRecord])
def list_memories(
    category: str | None = Query(default=None, max_length=50),
    kind: MemoryKind | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: AppContext = Depends(get_ctx),
) -> list[MemoryRecord]:
    """Memories, newest first, optionally filtered by category and kind."""
    return ctx.memory.retrieve(
        category=_category_query(category), kind=kind, limit=limit, offset=offset
    )


@router.get("/categories", response_model=list[CategoryCount])
def list_categories(ctx: AppContext = Depends(get_ctx)) -> list[CategoryCount]:
    return ctx.memory.categories()


@router.get("/search", response_model=list[MemoryRecord])
def search_memory(
    q: str = Query(min_length=2, max_length=500),
    limit: int = Query(default=10, ge=1, le=50),
    category: str | None = Query(default=None, max_length=50),
    ctx: AppContext = Depends(get_ctx),
) -> list[MemoryRecord]:
    return ctx.memory.search(q, limit=limit, category=_category_query(category))


@router.post("", response_model=MemoryRecord, status_code=status.HTTP_201_CREATED)
def create_note(body: NoteCreate, ctx: AppContext = Depends(get_ctx)) -> MemoryRecord:
    """Save a user note. Notes are given to the planner when they match a goal."""
    return ctx.memory.save(
        MemoryCreate(kind=MemoryKind.NOTE, content=body.content, category=body.category)
    )


@router.get("/{memory_id}", response_model=MemoryRecord)
def get_memory(memory_id: int, ctx: AppContext = Depends(get_ctx)) -> MemoryRecord:
    found = ctx.memory.retrieve(memory_id=memory_id, limit=1)
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Memory not found")
    return found[0]


@router.patch("/{memory_id}", response_model=MemoryRecord)
def update_memory(
    memory_id: int, body: MemoryUpdate, ctx: AppContext = Depends(get_ctx)
) -> MemoryRecord:
    updated = ctx.memory.update(
        memory_id,
        content=body.content.strip() if body.content is not None else None,
        category=body.category,
    )
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Memory not found")
    return updated


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_memory(memory_id: int, ctx: AppContext = Depends(get_ctx)) -> None:
    if not ctx.memory.delete(memory_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Memory not found")
