"""FastAPI application entry point.

Run locally with::

    uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.agent.llm import LLMClient
from app.api.routes import protected_router, public_router
from app.config import PROJECT_ROOT, Settings, get_settings
from app.context import build_context
from app.database.database import DatabaseError
from app.logging_config import configure_logging

logger = logging.getLogger(__name__)
DASHBOARD_DIR = PROJECT_ROOT / "dashboard"


def create_app(settings: Settings | None = None, llm: LLMClient | None = None) -> FastAPI:
    """Application factory (tests inject settings and a fake LLM)."""
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.secret_values(), settings.log_json)
    ctx = build_context(settings, llm=llm)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        recovered = ctx.runner.recover()
        if recovered:
            logger.info("re-queued %s interrupted task(s)", recovered)
        if settings.enable_monitor_scheduler:
            ctx.scheduler.start()
        if not ctx.llm.is_configured:
            logger.warning("OpenAI is not configured; tasks cannot be created until it is.")
        yield
        await ctx.scheduler.stop()
        await ctx.runner.shutdown()
        ctx.db.dispose()

    app = FastAPI(
        title="Dot Lab",
        version=__version__,
        description=(
            "An experimental always-on AI agent. Independent open-source project; "
            "not affiliated with, endorsed by, or an implementation of OpenAI Dots."
        ),
        lifespan=lifespan,
    )
    app.state.ctx = ctx

    @app.exception_handler(DatabaseError)
    async def _db_error(_: Request, exc: DatabaseError) -> JSONResponse:
        logger.error("database error", extra={"error": str(exc)})
        return JSONResponse(status_code=503, content={"detail": "Database error"})

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled API error")
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    app.include_router(public_router)
    app.include_router(protected_router)
    if DASHBOARD_DIR.is_dir():
        app.mount("/", StaticFiles(directory=DASHBOARD_DIR, html=True), name="dashboard")
    return app


app = create_app()
