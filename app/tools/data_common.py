"""Shared helpers for the data tools (CSV analyzer, data summary).

* :func:`load_csv` reads a CSV from the sandboxed workspace with size, type and
  encoding checks, so every data tool enforces the same rules.
* :class:`RowFilter` and :func:`apply_filters` give the LLM a small, safe filter
  language (no ``eval``/``query`` strings are ever executed).
* :func:`to_markdown` renders small tables without extra dependencies.
"""

from __future__ import annotations

from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, Field

from app.tools.base import ToolError, ToolInputError
from app.tools.filesystem import Workspace

MAX_COLUMNS = 200


class RowFilter(BaseModel):
    """Keep only rows where ``column <op> value``."""

    column: str = Field(min_length=1, max_length=200)
    op: Literal["==", "!=", ">", ">=", "<", "<=", "contains", "in"] = Field(
        description="Comparison. 'contains' is case-insensitive text matching; 'in' needs a list."
    )
    value: str | float | int | list[str | float | int] = Field(
        description="Value to compare with. ISO dates (YYYY-MM-DD) compare correctly as text."
    )


def load_csv(workspace: Workspace, path: str, max_bytes: int) -> pd.DataFrame:
    """Read a workspace CSV file into a DataFrame, or raise a user-safe ToolError."""
    target = workspace.resolve(path)
    rel = workspace.relative(target)
    if target.suffix.lower() != ".csv":
        raise ToolInputError(f"Only .csv files can be analysed (got '{rel}')")
    if not target.is_file():
        raise ToolError(f"File not found: {rel}")
    size = target.stat().st_size
    if size > max_bytes:
        raise ToolError(f"'{rel}' is {size:,} bytes; the limit is {max_bytes:,} (CSV_MAX_BYTES)")
    try:
        frame = pd.read_csv(target, encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ToolError(f"'{rel}' is not UTF-8 encoded text") from exc
    except pd.errors.EmptyDataError as exc:
        raise ToolError(f"'{rel}' is empty") from exc
    except pd.errors.ParserError as exc:
        raise ToolError(f"'{rel}' could not be parsed as CSV: {str(exc)[:200]}") from exc
    if frame.shape[1] > MAX_COLUMNS:
        raise ToolError(f"'{rel}' has {frame.shape[1]} columns; the limit is {MAX_COLUMNS}")
    frame.columns = [str(c).strip() for c in frame.columns]
    return frame


def require_columns(frame: pd.DataFrame, columns: list[str]) -> None:
    """Raise a helpful error that lists the real columns when a name is wrong."""
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        available = ", ".join(map(str, frame.columns[:50]))
        raise ToolInputError(f"Unknown column(s): {', '.join(missing)}. Available: {available}")


def numeric_column(frame: pd.DataFrame, column: str) -> pd.Series:
    """The column as numbers; raises if it has no numeric values at all."""
    require_columns(frame, [column])
    values = pd.to_numeric(frame[column], errors="coerce")
    if values.notna().sum() == 0:
        raise ToolInputError(f"Column '{column}' has no numeric values")
    return values


def is_numeric(series: pd.Series) -> bool:
    return pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series)


def apply_filters(frame: pd.DataFrame, filters: list[RowFilter]) -> pd.DataFrame:
    """Apply filters in order (all must match)."""
    require_columns(frame, [f.column for f in filters])
    for f in filters:
        series = frame[f.column]
        if f.op == "in":
            if not isinstance(f.value, list):
                raise ToolInputError(f"Filter on '{f.column}' with 'in' needs a list value")
            values = [_coerce(series, v) for v in f.value]
            mask = series.isin(values)
        elif f.op == "contains":
            if isinstance(f.value, list):
                raise ToolInputError(f"Filter on '{f.column}' with 'contains' needs one value")
            mask = series.astype(str).str.contains(str(f.value), case=False, regex=False)
        else:
            if isinstance(f.value, list):
                raise ToolInputError(f"Filter on '{f.column}' with '{f.op}' needs one value")
            value = _coerce(series, f.value)
            left = series if is_numeric(series) else series.astype(str)
            mask = {
                "==": left == value,
                "!=": left != value,
                ">": left > value,
                ">=": left >= value,
                "<": left < value,
                "<=": left <= value,
            }[f.op]
        frame = frame[mask.fillna(False)]
    return frame


def _coerce(series: pd.Series, value: Any) -> Any:
    """Compare numbers with numbers and text with text."""
    if is_numeric(series):
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ToolInputError(f"'{value}' is not a number") from exc
    return str(value)


def to_markdown(frame: pd.DataFrame, max_rows: int = 50) -> str:
    """Render a small DataFrame as a markdown table."""
    if frame.empty:
        return "(no rows)"
    shown = frame.head(max_rows)
    headers = [str(c) for c in shown.columns]
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for row in shown.itertuples(index=False):
        lines.append("| " + " | ".join(_cell(v) for v in row) + " |")
    if len(frame) > max_rows:
        lines.append(f"\n({len(frame) - max_rows} more rows not shown)")
    return "\n".join(lines)


def _cell(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, bool) or type(value).__name__ == "bool_":
        return "Yes" if value else "No"
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value).replace("|", "\\|").replace("\n", " ")


def records(frame: pd.DataFrame, max_rows: int = 100) -> list[dict[str, Any]]:
    """JSON-safe rows for ToolResult.data."""
    out = frame.head(max_rows).astype(object).where(frame.head(max_rows).notna(), None)
    return [{str(k): _json(v) for k, v in row.items()} for row in out.to_dict("records")]


def _json(value: Any) -> Any:
    if hasattr(value, "item"):  # numpy scalar
        return value.item()
    if isinstance(value, pd.Timestamp | pd.Period):
        return str(value)
    return value
