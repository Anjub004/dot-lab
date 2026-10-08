"""Structured logging with secret redaction.

Every log line is a JSON object (or a readable text line when ``LOG_JSON=false``)
with optional ``task_id``, ``event``, ``tool``, ``status`` and ``error`` fields.

Pass these fields through ``extra``::

    logger.info("tool finished", extra={"task_id": tid, "tool": "calculator"})

A redaction filter masks configured secret values and common credential
patterns (OpenAI-style keys, bearer tokens, ``api_key=...``) before anything is
written.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

STRUCTURED_FIELDS = ("task_id", "event", "tool", "status", "error")

_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|token|password|secret)\s*[=:]\s*[^\s,;&\"']+"),
    re.compile(r"tvly-[A-Za-z0-9_\-]{8,}"),
]

REDACTED = "[REDACTED]"


def redact(text: str, secrets: list[str] | None = None) -> str:
    """Mask known secret values and credential-looking substrings."""
    for secret in secrets or []:
        if secret and len(secret) >= 4:
            text = text.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


class RedactionFilter(logging.Filter):
    """Redacts secrets in the message and structured fields of each record."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage(), self._secrets)
        record.args = ()
        for field in STRUCTURED_FIELDS:
            value = getattr(record, field, None)
            if isinstance(value, str):
                setattr(record, field, redact(value, self._secrets))
        return True


class JsonFormatter(logging.Formatter):
    """Formats records as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in STRUCTURED_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable formatter that still shows structured fields."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = [
            f"{field}={getattr(record, field)}"
            for field in STRUCTURED_FIELDS
            if getattr(record, field, None) is not None
        ]
        return f"{base} {' '.join(extras)}".rstrip()


def configure_logging(level: str, secrets: list[str], json_output: bool = True) -> None:
    """Configure the root logger once for the whole application."""
    handler = logging.StreamHandler()
    if json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(TextFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactionFilter(secrets))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    # Third-party HTTP clients can log full URLs/headers at DEBUG; keep them quiet.
    for noisy in ("httpx", "httpcore", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
