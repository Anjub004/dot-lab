"""Notification tool.

Channels:

* ``dashboard`` — stores the notification in SQLite; it appears in the
  dashboard. Internal only, so no approval is needed.
* ``webhook`` — POSTs JSON ``{"title", "message", "task_id"}`` to
  ``NOTIFICATION_WEBHOOK_URL`` (e.g. a Slack/Discord/ntfy-compatible relay).
  This is external communication, so it ALWAYS requires human approval.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.database.database import Database
from app.database.models import NotificationRow
from app.tools.base import (
    BaseTool,
    ToolConfigurationError,
    ToolContext,
    ToolError,
    ToolResult,
)


class Notification(BaseModel):
    id: int
    title: str
    message: str
    channel: str
    task_id: str | None
    monitor_id: str | None
    read: bool
    created_at: datetime


class NotificationStore:
    """Persists dashboard notifications."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def create(
        self,
        title: str,
        message: str,
        channel: str = "dashboard",
        task_id: str | None = None,
        monitor_id: str | None = None,
    ) -> Notification:
        row = NotificationRow(
            title=title[:200],
            message=message,
            channel=channel,
            task_id=task_id,
            monitor_id=monitor_id,
        )
        with self._db.session() as session:
            session.add(row)
            session.flush()
            return Notification.model_validate(row, from_attributes=True)

    def list(self, limit: int = 50, unread_only: bool = False) -> list[Notification]:
        stmt = select(NotificationRow).order_by(NotificationRow.id.desc()).limit(limit)
        if unread_only:
            stmt = stmt.where(NotificationRow.read.is_(False))
        with self._db.session() as session:
            return [
                Notification.model_validate(r, from_attributes=True) for r in session.scalars(stmt)
            ]

    def mark_read(self, notification_id: int) -> bool:
        with self._db.session() as session:
            row = session.get(NotificationRow, notification_id)
            if row is None:
                return False
            row.read = True
            return True


class WebhookSender:
    """Sends a JSON payload to a configured webhook URL."""

    def __init__(
        self,
        url: str | None,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url
        self.timeout = timeout
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.url)

    async def send(self, payload: dict[str, object]) -> None:
        if not self.url:
            raise ToolConfigurationError(
                "Webhook channel is not configured. Set NOTIFICATION_WEBHOOK_URL in .env."
            )
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
                response = await client.post(self.url, json=payload)
        except httpx.TimeoutException as exc:
            raise ToolError("Webhook timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ToolError(f"Webhook network error: {type(exc).__name__}", retryable=True) from exc
        if response.status_code >= 500:
            raise ToolError(f"Webhook server error (HTTP {response.status_code})", retryable=True)
        if response.status_code >= 400:
            raise ToolError(f"Webhook rejected the request (HTTP {response.status_code})")


class NotificationInput(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=5000)
    channel: Literal["dashboard", "webhook"] = Field(
        default="dashboard",
        description="'dashboard' (internal) or 'webhook' (external, requires approval).",
    )


class NotificationTool(BaseTool):
    name = "notification"
    description = (
        "Notify the user. 'dashboard' shows the message in the Dot Lab dashboard; "
        "'webhook' sends it to an external webhook and always needs human approval."
    )
    input_model = NotificationInput

    def __init__(self, store: NotificationStore, webhook: WebhookSender) -> None:
        self.store = store
        self.webhook = webhook

    def approval_reason(self, args: BaseModel) -> str | None:
        params = self.typed(args, NotificationInput)
        if params.channel == "webhook":
            return "Sending a message to an external webhook is external communication."
        return None

    async def execute(self, args: BaseModel, context: ToolContext) -> ToolResult:
        params = self.typed(args, NotificationInput)
        if params.channel == "webhook":
            await self.webhook.send(
                {"title": params.title, "message": params.message, "task_id": context.task_id}
            )
            return ToolResult(
                content=f"Webhook notification sent: {params.title}",
                data={"channel": "webhook", "title": params.title},
            )
        note = self.store.create(
            params.title, params.message, channel="dashboard", task_id=context.task_id
        )
        return ToolResult(
            content=f"Dashboard notification created: {params.title}",
            data={"channel": "dashboard", "notification_id": note.id},
        )
