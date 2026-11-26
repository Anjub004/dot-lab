"""Reports saved by the report_generator tool (``<workspace>/reports/*.md``)."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from app.api.deps import get_ctx
from app.context import AppContext
from app.tools.filesystem import PathSecurityError, Workspace
from app.tools.report_generator import REPORTS_DIR

router = APIRouter(prefix="/reports", tags=["reports"])

# Same rule as the report generator's filename, plus the .md extension.
_REPORT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,79}\.md$")
MAX_REPORTS = 500


class ReportFile(BaseModel):
    name: str
    bytes: int
    modified_at: datetime


def _reports_dir(ctx: AppContext) -> Workspace:
    return Workspace(ctx.settings.workspace_dir)


@router.get("", response_model=list[ReportFile])
def list_reports(ctx: AppContext = Depends(get_ctx)) -> list[ReportFile]:
    """Saved reports, newest first."""
    folder = ctx.settings.workspace_dir / REPORTS_DIR
    if not folder.is_dir():
        return []
    files = [p for p in folder.iterdir() if p.is_file() and _REPORT_NAME.match(p.name)]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        ReportFile(
            name=p.name,
            bytes=p.stat().st_size,
            modified_at=datetime.fromtimestamp(p.stat().st_mtime, tz=UTC),
        )
        for p in files[:MAX_REPORTS]
    ]


@router.get("/{name}", response_class=Response)
def get_report(name: str, download: bool = True, ctx: AppContext = Depends(get_ctx)) -> Response:
    """One saved report (attachment by default; ``download=false`` to view)."""
    if not _REPORT_NAME.match(name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found")
    try:
        path = _reports_dir(ctx).resolve(f"{REPORTS_DIR}/{name}")
    except PathSecurityError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found") from exc
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found")
    disposition = "attachment" if download else "inline"
    return Response(
        content=path.read_text(encoding="utf-8", errors="replace"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'{disposition}; filename="{name}"'},
    )
