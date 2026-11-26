"""CSV analyzer tool: profile a CSV, aggregate it, or list its top rows.

All work is done with pandas on a CSV inside the sandboxed workspace. The LLM
chooses an operation and structured parameters; no code or query strings from
the LLM are ever executed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

import pandas as pd
from pydantic import BaseModel, Field, model_validator

from app.tools.base import BaseTool, PermissionLevel, ToolContext, ToolInputError, ToolResult
from app.tools.data_common import (
    RowFilter,
    apply_filters,
    is_numeric,
    load_csv,
    numeric_column,
    records,
    require_columns,
    to_markdown,
)
from app.tools.filesystem import Workspace

MAX_PROFILE_COLUMNS = 200


class Metric(BaseModel):
    column: str = Field(min_length=1, max_length=200)
    agg: Literal["sum", "mean", "median", "min", "max", "count", "nunique"]


class CsvAnalyzerInput(BaseModel):
    path: str = Field(max_length=255, description="Workspace-relative path to a .csv file.")
    operation: Literal["profile", "aggregate", "top_rows"] = Field(
        description=(
            "'profile': columns, types, missing values and basic statistics. "
            "'aggregate': group rows and compute metrics. "
            "'top_rows': rows sorted by a column."
        )
    )
    filters: list[RowFilter] = Field(default_factory=list, max_length=10)
    group_by: list[str] = Field(
        default_factory=list, max_length=5, description="Columns to group by (aggregate)."
    )
    metrics: list[Metric] = Field(
        default_factory=list, max_length=10, description="Metrics to compute (aggregate)."
    )
    columns: list[str] = Field(
        default_factory=list, max_length=20, description="Columns to show (top_rows)."
    )
    sort_by: str | None = Field(default=None, description="Column to sort by.")
    descending: bool = True
    limit: int = Field(default=20, ge=1, le=100, description="Maximum rows to return.")

    @model_validator(mode="after")
    def _check_operation(self) -> CsvAnalyzerInput:
        if self.operation == "aggregate" and not self.metrics:
            raise ValueError("'aggregate' needs at least one metric")
        if self.operation == "top_rows" and not self.sort_by:
            raise ValueError("'top_rows' needs sort_by")
        return self


class CsvAnalyzerTool(BaseTool):
    name = "csv_analyzer"
    description = (
        "Analyse a CSV file in the workspace with pandas: 'profile' it (columns, types, "
        "missing values, statistics), 'aggregate' it (group by columns and compute "
        "sum/mean/median/min/max/count/nunique), or list 'top_rows' sorted by a column. "
        "Supports simple row filters."
    )
    input_model = CsvAnalyzerInput
    permission = PermissionLevel.READ
    timeout_seconds = 20.0

    def __init__(self, workspace_dir: Path, max_bytes: int) -> None:
        self.workspace = Workspace(workspace_dir)
        self.max_bytes = max_bytes

    async def execute(self, args: BaseModel, context: ToolContext) -> ToolResult:
        params = self.typed(args, CsvAnalyzerInput)
        # pandas is CPU-bound: run it in a thread so the event loop stays responsive.
        return await asyncio.to_thread(self._run, params)

    def _run(self, params: CsvAnalyzerInput) -> ToolResult:
        frame = load_csv(self.workspace, params.path, self.max_bytes)
        total_rows = len(frame)
        frame = apply_filters(frame, params.filters)
        header = f"File: {params.path} · {total_rows:,} rows"
        if params.filters:
            header += f" · {len(frame):,} rows after filters"
        if params.operation == "profile":
            return profile(frame, header)
        if params.operation == "aggregate":
            return aggregate(frame, params, header)
        return top_rows(frame, params, header)


def profile(frame: pd.DataFrame, header: str) -> ToolResult:
    columns = []
    for name in frame.columns:
        series = frame[name]
        info: dict[str, object] = {
            "column": name,
            "type": "number" if is_numeric(series) else "text",
            "non_null": int(series.notna().sum()),
            "missing": int(series.isna().sum()),
            "unique": int(series.nunique(dropna=True)),
        }
        if is_numeric(series) and series.notna().any():
            info.update(
                sum=float(series.sum()),
                mean=float(series.mean()),
                min=float(series.min()),
                max=float(series.max()),
            )
        else:
            top = series.astype(str).value_counts().head(5)
            info["top_values"] = ", ".join(f"{v} ({c})" for v, c in top.items())
        columns.append(info)
    table = pd.DataFrame(columns)
    content = f"{header} · {frame.shape[1]} columns\n\n{to_markdown(table, max_rows=60)}"
    return ToolResult(
        content=content,
        data={"rows": len(frame), "columns": records(table, max_rows=MAX_PROFILE_COLUMNS)},
    )


def aggregate(frame: pd.DataFrame, params: CsvAnalyzerInput, header: str) -> ToolResult:
    require_columns(frame, params.group_by)
    work = frame.copy()
    named: dict[str, tuple[str, str]] = {}
    for metric in params.metrics:
        if metric.agg not in ("count", "nunique"):
            work[metric.column] = numeric_column(work, metric.column)
        else:
            require_columns(work, [metric.column])
        named[f"{metric.agg}_{metric.column}"] = (metric.column, metric.agg)

    if params.group_by:
        result = work.groupby(params.group_by, dropna=False).agg(**named).reset_index()
    else:
        result = pd.DataFrame([{name: work[col].agg(agg) for name, (col, agg) in named.items()}])

    sort_by = params.sort_by or next(iter(named))
    if sort_by not in result.columns:
        raise ToolInputError(
            f"sort_by '{sort_by}' is not in the result. Use one of: {', '.join(result.columns)}"
        )
    result = result.sort_values(sort_by, ascending=not params.descending)
    groups = len(result)
    result = result.head(params.limit)
    shown = f"top {len(result)} of {groups} groups" if groups > len(result) else f"{groups} groups"
    content = f"{header} · {shown}, sorted by {sort_by}\n\n{to_markdown(result, params.limit)}"
    return ToolResult(
        content=content,
        data={"groups": groups, "rows": records(result, params.limit), "sort_by": sort_by},
    )


def top_rows(frame: pd.DataFrame, params: CsvAnalyzerInput, header: str) -> ToolResult:
    sort_by = params.sort_by or ""
    columns = params.columns or list(frame.columns)
    require_columns(frame, [sort_by, *columns])
    key = frame[sort_by]
    if not is_numeric(key):
        numeric = pd.to_numeric(key, errors="coerce")
        key = numeric if numeric.notna().any() else key.astype(str)
    order = key.sort_values(ascending=not params.descending, na_position="last").index
    result = frame.loc[order, columns].head(params.limit)
    direction = "highest" if params.descending else "lowest"
    content = (
        f"{header} · {len(result)} rows with the {direction} {sort_by}\n\n"
        f"{to_markdown(result, params.limit)}"
    )
    return ToolResult(content=content, data={"rows": records(result, params.limit)})
