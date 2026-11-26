"""Data summary generator: business summary of one metric in a CSV.

Given a numeric metric (e.g. ``revenue``), it calculates:

* the overall total, average per row and row count,
* totals per day / week / month when a date column is given, the change of the
  latest period versus the previous one, and the best and worst periods,
* the top and bottom values of a dimension (e.g. ``branch``) with their share.

Partial periods (a month that is only partly covered by the data) are flagged,
because comparing a partial month with a full one is misleading.
"""

from __future__ import annotations

import asyncio
import warnings
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, Field

from app.tools.base import BaseTool, PermissionLevel, ToolContext, ToolInputError, ToolResult
from app.tools.data_common import (
    RowFilter,
    apply_filters,
    load_csv,
    numeric_column,
    records,
    require_columns,
    to_markdown,
)
from app.tools.filesystem import Workspace

PERIOD_CODES = {"day": "D", "week": "W", "month": "M"}


class DataSummaryInput(BaseModel):
    path: str = Field(max_length=255, description="Workspace-relative path to a .csv file.")
    metric: str = Field(min_length=1, max_length=200, description="Numeric column to summarise.")
    date_column: str | None = Field(default=None, description="Date column for trends.")
    period: Literal["day", "week", "month"] = "month"
    dimension: str | None = Field(
        default=None, description="Column to rank, e.g. 'branch' or 'category'."
    )
    top_n: int = Field(default=5, ge=1, le=20)
    filters: list[RowFilter] = Field(default_factory=list, max_length=10)


class DataSummaryTool(BaseTool):
    name = "data_summary"
    description = (
        "Summarise one numeric metric of a CSV file for a business report: total, "
        "average, trend by day/week/month with the latest period-over-period change "
        "(partial periods are flagged), and the top/bottom values of a dimension with "
        "their share of the total."
    )
    input_model = DataSummaryInput
    permission = PermissionLevel.READ
    timeout_seconds = 20.0

    def __init__(self, workspace_dir: Path, max_bytes: int) -> None:
        self.workspace = Workspace(workspace_dir)
        self.max_bytes = max_bytes

    async def execute(self, args: BaseModel, context: ToolContext) -> ToolResult:
        params = self.typed(args, DataSummaryInput)
        return await asyncio.to_thread(self._run, params)

    def _run(self, params: DataSummaryInput) -> ToolResult:
        frame = apply_filters(load_csv(self.workspace, params.path, self.max_bytes), params.filters)
        if frame.empty:
            raise ToolInputError("No rows left to summarise (check the filters)")
        values = numeric_column(frame, params.metric)
        frame = frame.assign(_metric=values)
        skipped = int(values.isna().sum())

        data: dict[str, Any] = {
            "metric": params.metric,
            "rows": len(frame),
            "total": float(values.sum()),
            "average_per_row": float(values.mean()),
            "non_numeric_rows_skipped": skipped,
        }
        parts = [
            f"## Summary of '{params.metric}' ({params.path})",
            f"- Total: {data['total']:,.2f}",
            f"- Average per row: {data['average_per_row']:,.2f} over {len(frame):,} rows",
        ]
        if skipped:
            parts.append(f"- {skipped:,} row(s) had no numeric value and were skipped")

        if params.date_column:
            parts.append(self._trend(frame, params, data))
        if params.dimension:
            parts.append(self._ranking(frame, params, data))
        return ToolResult(content="\n".join(parts), data=data)

    @staticmethod
    def _trend(frame: pd.DataFrame, params: DataSummaryInput, data: dict[str, Any]) -> str:
        column = params.date_column or ""
        require_columns(frame, [column])
        with warnings.catch_warnings():
            # pandas warns when it cannot infer one date format; unparseable values
            # simply become NaT, which is handled below.
            warnings.simplefilter("ignore", UserWarning)
            dates = pd.to_datetime(frame[column], errors="coerce")
        if dates.notna().sum() == 0:
            raise ToolInputError(f"Column '{column}' has no valid dates")
        periods = dates.dt.to_period(PERIOD_CODES[params.period])
        grouped = (
            frame.assign(_period=periods, _day=dates.dt.normalize())
            .dropna(subset=["_period"])
            .groupby("_period")
        )
        table = grouped.agg(_total=("_metric", "sum"), days=("_day", "nunique")).reset_index()
        # Per-day average makes periods of different length comparable (28- vs 31-day months).
        table["per_day"] = (table["_total"] / table["days"]).round(2)
        table = table.rename(columns={"_period": params.period, "_total": params.metric})
        first, last = dates.min().normalize(), dates.max().normalize()
        table["partial"] = [
            p.start_time.normalize() < first or p.end_time.normalize() > last
            for p in table[params.period]
        ]
        table[params.period] = table[params.period].astype(str)

        lines = [f"\n### Trend by {params.period}", to_markdown(table, max_rows=60)]
        complete = table[~table["partial"]]
        if len(complete) >= 2:
            latest, previous = complete.iloc[-1], complete.iloc[-2]
            change = latest[params.metric] - previous[params.metric]
            pct = change / previous[params.metric] * 100 if previous[params.metric] else None
            pct_text = f" ({pct:+.1f}%)" if pct is not None else ""
            lines.append(
                f"- Latest complete {params.period} {latest[params.period]}: "
                f"{latest[params.metric]:,.2f}, change vs {previous[params.period]}: "
                f"{change:+,.2f}{pct_text}"
            )
            data["latest_change_pct"] = round(pct, 2) if pct is not None else None
            if previous["per_day"]:
                per_day_pct = (latest["per_day"] / previous["per_day"] - 1) * 100
                lines.append(
                    f"- Per day: {latest['per_day']:,.2f} vs {previous['per_day']:,.2f} "
                    f"({per_day_pct:+.1f}%), which adjusts for different period lengths"
                )
                data["latest_change_per_day_pct"] = round(per_day_pct, 2)
        if len(complete):
            best = complete.loc[complete["per_day"].idxmax()]
            worst = complete.loc[complete["per_day"].idxmin()]
            lines.append(
                f"- Best complete {params.period} by per-day average: {best[params.period]} "
                f"({best['per_day']:,.2f}/day); worst: {worst[params.period]} "
                f"({worst['per_day']:,.2f}/day)"
            )
        if table["partial"].any():
            partial = ", ".join(table.loc[table["partial"], params.period])
            lines.append(
                f"- Partial {params.period}s (data does not cover the whole period, "
                f"excluded from comparisons): {partial}"
            )
        data["trend"] = records(table, max_rows=400)
        return "\n".join(lines)

    @staticmethod
    def _ranking(frame: pd.DataFrame, params: DataSummaryInput, data: dict[str, Any]) -> str:
        column = params.dimension or ""
        require_columns(frame, [column])
        totals = frame.groupby(column, dropna=False)["_metric"].sum().sort_values(ascending=False)
        grand = totals.sum()
        table = totals.reset_index().rename(columns={"_metric": params.metric})
        table["share_pct"] = (table[params.metric] / grand * 100).round(1) if grand else None
        n = params.top_n
        lines = [
            f"\n### {column} ranked by {params.metric} ({len(table)} values)",
            f"Top {min(n, len(table))}:",
            to_markdown(table.head(n), n),
        ]
        if len(table) > n:
            bottom = table.iloc[max(n, len(table) - n) :]  # never repeat rows shown in the top
            lines += [f"\nBottom {len(bottom)}:", to_markdown(bottom, n)]
        data["ranking"] = records(table, max_rows=200)
        return "\n".join(lines)
