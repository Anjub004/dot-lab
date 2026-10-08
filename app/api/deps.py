"""Shared FastAPI dependencies."""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader

from app.context import AppContext

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def require_api_key(
    ctx: AppContext = Depends(get_ctx), provided: str | None = Depends(api_key_header)
) -> None:
    """If DOT_LAB_API_KEY is set, every /api call must send a matching X-API-Key header."""
    expected = ctx.settings.dot_lab_api_key
    if expected is None:
        return
    if not provided or not secrets.compare_digest(provided, expected.get_secret_value()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid X-API-Key header")
