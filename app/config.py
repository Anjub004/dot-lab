"""Application configuration.

Settings are read from environment variables (optionally loaded from a ``.env``
file via python-dotenv). Secrets are wrapped in :class:`pydantic.SecretStr` so
they are never printed by accident in logs, reprs, or API responses.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr, field_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _csv(value: str | None) -> list[str]:
    """Split a comma-separated environment value into a clean list."""
    if not value:
        return []
    return [item.strip().lower() for item in value.split(",") if item.strip()]


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int) -> int:
    if value is None or value.strip() == "":
        return default
    return int(value)


def _secret(value: str | None) -> SecretStr | None:
    if value is None or value.strip() == "":
        return None
    return SecretStr(value.strip())


class Settings(BaseModel):
    """All runtime configuration for Dot Lab."""

    # --- AI -----------------------------------------------------------------
    openai_api_key: SecretStr | None = None
    openai_model: str | None = None
    openai_timeout_seconds: int = Field(default=60, ge=1, le=600)
    openai_max_retries: int = Field(default=2, ge=0, le=10)

    # --- Storage ------------------------------------------------------------
    database_url: str = f"sqlite:///{PROJECT_ROOT / 'data' / 'dot_lab.db'}"
    workspace_dir: Path = PROJECT_ROOT / "data" / "workspace"
    max_file_bytes: int = Field(default=1_000_000, ge=1)

    # --- Web search ---------------------------------------------------------
    search_provider: Literal["none", "tavily", "brave"] = "none"
    search_api_key: SecretStr | None = None
    search_max_results: int = Field(default=5, ge=1, le=10)

    # --- Browser ------------------------------------------------------------
    browser_backend: Literal["httpx", "playwright"] = "httpx"
    browser_allowed_domains: list[str] = Field(default_factory=list)
    browser_allow_private_networks: bool = False
    browser_max_chars: int = Field(default=20_000, ge=500)
    browser_max_bytes: int = Field(default=2_000_000, ge=10_000)

    # --- Notifications ------------------------------------------------------
    notification_webhook_url: SecretStr | None = None

    # --- Agent limits -------------------------------------------------------
    max_agent_iterations: int = Field(default=30, ge=1, le=500)
    max_plan_steps: int = Field(default=8, ge=1, le=30)
    max_step_retries: int = Field(default=2, ge=0, le=10)
    max_replans: int = Field(default=1, ge=0, le=5)
    tool_timeout_seconds: int = Field(default=30, ge=1, le=600)
    task_timeout_seconds: int = Field(default=900, ge=10)
    max_concurrent_tasks: int = Field(default=2, ge=1, le=32)
    max_goal_length: int = Field(default=2000, ge=10)
    approval_required_tools: list[str] = Field(default_factory=list)
    approval_rejection_policy: Literal["skip", "fail"] = "skip"

    # --- Monitoring ---------------------------------------------------------
    enable_monitor_scheduler: bool = True
    monitor_poll_seconds: int = Field(default=30, ge=1)
    monitor_min_interval_minutes: int = Field(default=5, ge=1)

    # --- Server -------------------------------------------------------------
    dot_lab_api_key: SecretStr | None = None
    log_level: str = "INFO"
    log_json: bool = True

    @field_validator("workspace_dir")
    @classmethod
    def _resolve_workspace(cls, value: Path) -> Path:
        path = Path(value)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path.resolve()

    @field_validator("database_url")
    @classmethod
    def _resolve_sqlite_path(cls, value: str) -> str:
        """Make relative SQLite paths relative to the project root, not the CWD."""
        prefix = "sqlite:///"
        if value.startswith(prefix) and ":memory:" not in value:
            path = Path(value[len(prefix) :])
            if not path.is_absolute():
                return f"{prefix}{(PROJECT_ROOT / path).resolve()}"
        return value

    @property
    def llm_configured(self) -> bool:
        """True when both an API key and a model name are present."""
        return self.openai_api_key is not None and bool(self.openai_model)

    def secret_values(self) -> list[str]:
        """Every configured secret, used by the log redaction filter."""
        secrets = [
            self.openai_api_key,
            self.search_api_key,
            self.notification_webhook_url,
            self.dot_lab_api_key,
        ]
        return [s.get_secret_value() for s in secrets if s is not None]

    @classmethod
    def from_env(cls, env_file: str | Path | None = None) -> Settings:
        """Build settings from environment variables (and an optional .env)."""
        load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
        env = os.environ
        data: dict[str, object] = {
            "openai_api_key": _secret(env.get("OPENAI_API_KEY")),
            "openai_model": (env.get("OPENAI_MODEL") or "").strip() or None,
            "openai_timeout_seconds": _int(env.get("OPENAI_TIMEOUT_SECONDS"), 60),
            "openai_max_retries": _int(env.get("OPENAI_MAX_RETRIES"), 2),
            "search_provider": (env.get("SEARCH_PROVIDER") or "none").strip().lower(),
            "search_api_key": _secret(env.get("SEARCH_API_KEY")),
            "search_max_results": _int(env.get("SEARCH_MAX_RESULTS"), 5),
            "browser_backend": (env.get("BROWSER_BACKEND") or "httpx").strip().lower(),
            "browser_allowed_domains": _csv(env.get("BROWSER_ALLOWED_DOMAINS")),
            "browser_allow_private_networks": _bool(
                env.get("BROWSER_ALLOW_PRIVATE_NETWORKS"), False
            ),
            "browser_max_chars": _int(env.get("BROWSER_MAX_CHARS"), 20_000),
            "notification_webhook_url": _secret(env.get("NOTIFICATION_WEBHOOK_URL")),
            "max_agent_iterations": _int(env.get("MAX_AGENT_ITERATIONS"), 30),
            "max_plan_steps": _int(env.get("MAX_PLAN_STEPS"), 8),
            "max_step_retries": _int(env.get("MAX_STEP_RETRIES"), 2),
            "max_replans": _int(env.get("MAX_REPLANS"), 1),
            "tool_timeout_seconds": _int(env.get("TOOL_TIMEOUT_SECONDS"), 30),
            "task_timeout_seconds": _int(env.get("TASK_TIMEOUT_SECONDS"), 900),
            "max_concurrent_tasks": _int(env.get("MAX_CONCURRENT_TASKS"), 2),
            "max_goal_length": _int(env.get("MAX_GOAL_LENGTH"), 2000),
            "approval_required_tools": _csv(env.get("APPROVAL_REQUIRED_TOOLS")),
            "approval_rejection_policy": (env.get("APPROVAL_REJECTION_POLICY") or "skip")
            .strip()
            .lower(),
            "enable_monitor_scheduler": _bool(env.get("ENABLE_MONITOR_SCHEDULER"), True),
            "monitor_poll_seconds": _int(env.get("MONITOR_POLL_SECONDS"), 30),
            "monitor_min_interval_minutes": _int(env.get("MONITOR_MIN_INTERVAL_MINUTES"), 5),
            "dot_lab_api_key": _secret(env.get("DOT_LAB_API_KEY")),
            "log_level": (env.get("LOG_LEVEL") or "INFO").upper(),
            "log_json": _bool(env.get("LOG_JSON"), True),
        }
        if env.get("DATABASE_URL"):
            data["database_url"] = env["DATABASE_URL"].strip()
        if env.get("WORKSPACE_DIR"):
            data["workspace_dir"] = Path(env["WORKSPACE_DIR"].strip())
        return cls(**data)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings.from_env()
