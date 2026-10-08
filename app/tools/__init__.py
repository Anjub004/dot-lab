"""Agent tools and the default tool registry."""

from __future__ import annotations

from app.config import Settings
from app.tools.base import (
    BaseTool,
    ToolConfigurationError,
    ToolContext,
    ToolError,
    ToolInputError,
    ToolRegistry,
    ToolResult,
)
from app.tools.browser import BrowserTool, UrlPolicy, build_browser_backend
from app.tools.calculator import CalculatorTool
from app.tools.filesystem import FileSystemTool
from app.tools.notifications import NotificationStore, NotificationTool, WebhookSender
from app.tools.web_search import WebSearchTool, build_search_provider


def build_default_registry(settings: Settings, notifications: NotificationStore) -> ToolRegistry:
    """Create the registry with the five built-in V1 tools."""
    search_key = settings.search_api_key.get_secret_value() if settings.search_api_key else None
    webhook = (
        settings.notification_webhook_url.get_secret_value()
        if settings.notification_webhook_url
        else None
    )
    policy = UrlPolicy(settings.browser_allowed_domains, settings.browser_allow_private_networks)
    browser_backend = build_browser_backend(
        settings.browser_backend,
        policy,
        timeout=float(settings.tool_timeout_seconds),
        max_bytes=settings.browser_max_bytes,
    )
    return ToolRegistry(
        [
            CalculatorTool(),
            WebSearchTool(
                build_search_provider(settings.search_provider, search_key),
                default_max_results=settings.search_max_results,
            ),
            BrowserTool(browser_backend, max_chars=settings.browser_max_chars),
            FileSystemTool(settings.workspace_dir, max_bytes=settings.max_file_bytes),
            NotificationTool(notifications, WebhookSender(webhook)),
        ]
    )


__all__ = [
    "BaseTool",
    "ToolConfigurationError",
    "ToolContext",
    "ToolError",
    "ToolInputError",
    "ToolRegistry",
    "ToolResult",
    "build_default_registry",
]
