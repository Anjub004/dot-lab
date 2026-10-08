"""Logging must never leak credentials."""

from __future__ import annotations

import json
import logging

from app.logging_config import JsonFormatter, RedactionFilter, redact


def test_redact_patterns_and_known_secrets() -> None:
    text = (
        "key sk-proj-abcdefghijklmnop Authorization: Bearer abc.def.ghijklmnop "
        "api_key=xyz123456 my-custom-secret-value"
    )
    out = redact(text, secrets=["my-custom-secret-value"])
    for leaked in (
        "sk-proj-abcdefghijklmnop",
        "abc.def.ghijklmnop",
        "xyz123456",
        "my-custom-secret-value",
    ):
        assert leaked not in out


def test_structured_json_log_is_redacted() -> None:
    record = logging.LogRecord(
        "t", logging.ERROR, __file__, 1, "failed with sk-abcdefghijklmn", None, None
    )
    record.task_id = "task-1"
    record.tool = "web_search"
    record.error = "token=supersecret123"
    RedactionFilter(secrets=[]).filter(record)
    payload = json.loads(JsonFormatter().format(record))
    assert payload["task_id"] == "task-1" and payload["tool"] == "web_search"
    assert "sk-abcdefghijklmn" not in payload["message"]
    assert "supersecret123" not in payload["error"]
    assert {"timestamp", "level", "message"} <= set(payload)
