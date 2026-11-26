"""Tests for the data tools (CSV analyzer, data summary, report generator) and tool permissions."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.agent.executor import StepExecutor
from app.config import Settings
from app.database.database import Database
from app.tools import build_default_registry
from app.tools.base import (
    BaseTool,
    PermissionLevel,
    ToolContext,
    ToolError,
    ToolInputError,
    ToolRegistry,
    ToolResult,
)
from app.tools.calculator import CalculatorTool
from app.tools.csv_analyzer import CsvAnalyzerTool
from app.tools.data_summary import DataSummaryTool
from app.tools.filesystem import FileSystemTool, PathSecurityError
from app.tools.notifications import NotificationStore
from app.tools.report_generator import ReportGeneratorTool

CTX = ToolContext(task_id="t1", step_id="step_1")
DEMO = Path(__file__).resolve().parent.parent / "examples" / "data" / "demo_sales.csv"

SMALL_CSV = """date,branch,category,units_sold,revenue
2026-01-30,North,Dairy,10,100.0
2026-01-31,North,Bakery,5,50.5
2026-02-01,South,Dairy,8,80.0
2026-02-15,South,Bakery,,20.0
2026-02-28,East,Dairy,4,not-a-number
2026-03-01,East,Dairy,2,30.0
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "data").mkdir(parents=True)
    (ws / "data" / "small.csv").write_text(SMALL_CSV, encoding="utf-8")
    return ws


@pytest.fixture
def csv_tool(workspace: Path) -> CsvAnalyzerTool:
    return CsvAnalyzerTool(workspace, max_bytes=1_000_000)


@pytest.fixture
def summary_tool(workspace: Path) -> DataSummaryTool:
    return DataSummaryTool(workspace, max_bytes=1_000_000)


async def run(tool: BaseTool, raw: dict) -> ToolResult:
    return await tool.execute(tool.parse_input(raw), CTX)


# ------------------------------------------------------------ csv_analyzer --


async def test_csv_profile(csv_tool: CsvAnalyzerTool) -> None:
    result = await run(csv_tool, {"path": "data/small.csv", "operation": "profile"})
    assert result.data["rows"] == 6
    cols = {c["column"]: c for c in result.data["columns"]}
    assert cols["units_sold"]["type"] == "number" and cols["units_sold"]["missing"] == 1
    assert cols["units_sold"]["sum"] == 29
    assert cols["revenue"]["type"] == "text"  # one non-numeric value makes it text
    assert "Dairy (4)" in cols["category"]["top_values"]
    assert "| column |" in result.content


async def test_csv_aggregate_with_filters(csv_tool: CsvAnalyzerTool) -> None:
    result = await run(
        csv_tool,
        {
            "path": "data/small.csv",
            "operation": "aggregate",
            "group_by": ["branch"],
            "metrics": [
                {"column": "revenue", "agg": "sum"},
                {"column": "units_sold", "agg": "count"},
            ],
            "filters": [{"column": "date", "op": ">=", "value": "2026-02-01"}],
        },
    )
    rows = {r["branch"]: r for r in result.data["rows"]}
    # Non-numeric revenue is ignored; empty units_sold is not counted.
    assert rows == {
        "South": {"branch": "South", "sum_revenue": 100.0, "count_units_sold": 1},
        "East": {"branch": "East", "sum_revenue": 30.0, "count_units_sold": 2},
    }
    assert result.data["rows"][0]["branch"] == "South"  # sorted by first metric, descending


async def test_csv_aggregate_without_group_by(csv_tool: CsvAnalyzerTool) -> None:
    result = await run(
        csv_tool,
        {
            "path": "data/small.csv",
            "operation": "aggregate",
            "metrics": [{"column": "units_sold", "agg": "sum"}],
            "filters": [{"column": "branch", "op": "in", "value": ["North", "East"]}],
        },
    )
    assert result.data["rows"] == [{"sum_units_sold": 21.0}]


async def test_csv_top_rows(csv_tool: CsvAnalyzerTool) -> None:
    result = await run(
        csv_tool,
        {
            "path": "data/small.csv",
            "operation": "top_rows",
            "sort_by": "units_sold",
            "columns": ["branch", "units_sold"],
            "limit": 2,
        },
    )
    assert [r["units_sold"] for r in result.data["rows"]] == [10.0, 8.0]
    assert set(result.data["rows"][0]) == {"branch", "units_sold"}


async def test_csv_contains_filter(csv_tool: CsvAnalyzerTool) -> None:
    result = await run(
        csv_tool,
        {
            "path": "data/small.csv",
            "operation": "top_rows",
            "sort_by": "date",
            "filters": [{"column": "category", "op": "contains", "value": "BAK"}],
        },
    )
    assert {r["category"] for r in result.data["rows"]} == {"Bakery"}


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        ({"path": "data/small.csv", "operation": "aggregate"}, "at least one metric"),
        ({"path": "data/small.csv", "operation": "top_rows"}, "sort_by"),
        ({"path": "data/small.csv", "operation": "profile", "limit": 1000}, "less than"),
    ],
)
def test_csv_input_validation(csv_tool: CsvAnalyzerTool, raw: dict, error: str) -> None:
    with pytest.raises(ToolInputError, match=error):
        csv_tool.parse_input(raw)


async def test_csv_unknown_column_lists_available(csv_tool: CsvAnalyzerTool) -> None:
    with pytest.raises(ToolInputError, match=r"Unknown column\(s\): sales.*Available: date"):
        await run(
            csv_tool,
            {
                "path": "data/small.csv",
                "operation": "aggregate",
                "metrics": [{"column": "sales", "agg": "sum"}],
            },
        )


@pytest.mark.parametrize("path", ["../outside.csv", "/etc/passwd.csv", "data/../../x.csv"])
async def test_csv_path_traversal_blocked(csv_tool: CsvAnalyzerTool, path: str) -> None:
    with pytest.raises(PathSecurityError):
        await run(csv_tool, {"path": path, "operation": "profile"})


async def test_csv_file_checks(csv_tool: CsvAnalyzerTool, workspace: Path) -> None:
    (workspace / "notes.txt").write_text("a,b\n1,2\n")
    with pytest.raises(ToolInputError, match=r"Only \.csv"):
        await run(csv_tool, {"path": "notes.txt", "operation": "profile"})
    with pytest.raises(ToolError, match="File not found"):
        await run(csv_tool, {"path": "missing.csv", "operation": "profile"})
    (workspace / "empty.csv").write_text("")
    with pytest.raises(ToolError, match="empty"):
        await run(csv_tool, {"path": "empty.csv", "operation": "profile"})
    small = CsvAnalyzerTool(workspace, max_bytes=10)
    with pytest.raises(ToolError, match="CSV_MAX_BYTES"):
        await run(small, {"path": "data/small.csv", "operation": "profile"})


# ------------------------------------------------------------ data_summary --


async def test_data_summary_trend_and_ranking(summary_tool: DataSummaryTool) -> None:
    result = await run(
        summary_tool,
        {
            "path": "data/small.csv",
            "metric": "revenue",
            "date_column": "date",
            "period": "month",
            "dimension": "branch",
            "top_n": 1,
        },
    )
    data = result.data
    assert data["total"] == pytest.approx(280.5)
    assert data["non_numeric_rows_skipped"] == 1
    trend = {r["month"]: r for r in data["trend"]}
    assert trend["2026-01"]["revenue"] == pytest.approx(150.5)
    assert trend["2026-01"]["days"] == 2 and trend["2026-01"]["per_day"] == pytest.approx(75.25)
    # Data runs 30 Jan - 1 Mar: January and March are partial, February is complete.
    assert trend["2026-01"]["partial"] and trend["2026-03"]["partial"]
    assert not trend["2026-02"]["partial"]
    assert "latest_change_pct" not in data  # only one complete month: no comparison
    assert "Partial months" in result.content
    ranking = [r["branch"] for r in data["ranking"]]
    assert ranking == ["North", "South", "East"]
    assert "Top 1:" in result.content and "Bottom 1:" in result.content


async def test_data_summary_on_demo_data(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "demo").mkdir(parents=True)
    shutil.copy(DEMO, ws / "demo" / "demo_sales.csv")
    tool = DataSummaryTool(ws, max_bytes=20_000_000)
    result = await run(
        tool,
        {
            "path": "demo/demo_sales.csv",
            "metric": "gross_profit",
            "date_column": "date",
            "dimension": "branch",
        },
    )
    assert result.data["rows"] == 11_520
    assert len(result.data["ranking"]) == 8
    months = [r for r in result.data["trend"] if not r["partial"]]
    assert [m["month"] for m in months] == ["2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]
    assert isinstance(result.data["latest_change_pct"], float)
    assert isinstance(result.data["latest_change_per_day_pct"], float)
    assert "| partial |" in result.content and "| Yes |" in result.content


async def test_data_summary_errors(summary_tool: DataSummaryTool) -> None:
    with pytest.raises(ToolInputError, match="no numeric values"):
        await run(summary_tool, {"path": "data/small.csv", "metric": "branch"})
    with pytest.raises(ToolInputError, match="no valid dates"):
        await run(
            summary_tool,
            {"path": "data/small.csv", "metric": "units_sold", "date_column": "branch"},
        )
    with pytest.raises(ToolInputError, match="No rows left"):
        await run(
            summary_tool,
            {
                "path": "data/small.csv",
                "metric": "units_sold",
                "filters": [{"column": "branch", "op": "==", "value": "Nowhere"}],
            },
        )


# -------------------------------------------------------- report_generator --


async def test_report_generator_writes_and_protects_overwrite(workspace: Path) -> None:
    tool = ReportGeneratorTool(workspace, max_bytes=100_000)
    raw = {
        "title": "June Sales",
        "filename": "june_sales",
        "summary": "Revenue grew.",
        "sections": [{"heading": "By branch", "body": "| a | b |\n|---|---|\n| 1 | 2 |"}],
    }
    args = tool.parse_input(raw)
    assert tool.approval_reason(args) is None  # new file: no approval needed

    result = await tool.execute(args, CTX)
    text = (workspace / "reports" / "june_sales.md").read_text()
    assert result.data["path"] == "reports/june_sales.md"
    assert text.startswith("# June Sales\n") and "## Summary\n\nRevenue grew." in text
    assert "## By branch" in text and "Generated by Dot Lab" in text
    assert "overwrite" in (tool.approval_reason(args) or "")  # now it exists


@pytest.mark.parametrize("filename", ["../evil", "a/b", "report.md", "", ".hidden", "x" * 81])
def test_report_filename_is_restricted(workspace: Path, filename: str) -> None:
    tool = ReportGeneratorTool(workspace, max_bytes=100_000)
    with pytest.raises(ToolInputError):
        tool.parse_input(
            {"title": "t", "filename": filename, "sections": [{"heading": "h", "body": "b"}]}
        )


# -------------------------------------------------------------- permissions --


def test_permission_levels_of_builtin_tools(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 't.db'}")
    db.create_all()
    registry = build_default_registry(
        Settings(workspace_dir=tmp_path / "ws", database_url=db.url), NotificationStore(db)
    )
    levels = {d["name"]: d["permission"] for d in registry.describe()}
    assert levels == {
        "calculator": "read",
        "csv_analyzer": "read",
        "data_summary": "read",
        "filesystem": "write",
        "report_generator": "write",
        "web_search": "external",
        "browser": "external",
        "notification": "external",
    }


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("read", {"calculator", "csv_analyzer", "data_summary"}),
        ("write", {"calculator", "csv_analyzer", "data_summary", "filesystem", "report_generator"}),
    ],
)
def test_permission_level_limits_tools(tmp_path: Path, level: str, expected: set[str]) -> None:
    db = Database(f"sqlite:///{tmp_path / 't.db'}")
    db.create_all()
    settings = Settings(
        workspace_dir=tmp_path / "ws", database_url=db.url, tool_permission_level=level
    )
    registry = build_default_registry(settings, NotificationStore(db))
    assert {t.name for t in registry.available()} == expected
    with pytest.raises(ToolError, match="TOOL_PERMISSION_LEVEL"):
        registry.get("notification")
    assert {d["name"] for d in registry.describe() if not d["permitted"]} == (
        set(registry.names()) - expected - {"web_search"}  # web_search is unconfigured too
    ) | ({"web_search"} if level != "external" else set())


def test_undeclared_tools_are_treated_as_external() -> None:
    class Plain(BaseTool):
        name = "plain"
        description = "d"
        input_model = EmptyInput

        async def execute(self, args: BaseModel, context: ToolContext) -> ToolResult:
            return ToolResult(content="")

    assert Plain.permission is PermissionLevel.EXTERNAL
    registry = ToolRegistry([Plain()], max_permission=PermissionLevel.WRITE)
    assert registry.available() == []


# -------------------------------------------------------------- timeouts --


class EmptyInput(BaseModel):
    pass


class SlowTool(BaseTool):
    name = "slow"
    description = "sleeps"
    input_model = EmptyInput
    permission = PermissionLevel.READ
    timeout_seconds = 0.05

    async def execute(self, args: BaseModel, context: ToolContext) -> ToolResult:
        await asyncio.sleep(1)
        return ToolResult(content="done")


async def test_per_tool_timeout_never_exceeds_global_limit() -> None:
    executor = StepExecutor(llm=None, tool_timeout_seconds=30, max_tool_retries=0)  # type: ignore[arg-type]
    assert executor.timeout_for(SlowTool()) == 0.05
    assert executor.timeout_for(CalculatorTool()) == 5.0
    assert executor.timeout_for(FileSystemTool(Path("."), max_bytes=1)) == 30.0  # no own limit
    tight = StepExecutor(llm=None, tool_timeout_seconds=2, max_tool_retries=0)  # type: ignore[arg-type]
    assert tight.timeout_for(CalculatorTool()) == 2.0  # global limit wins when smaller

    run = await executor.run_tool(SlowTool(), EmptyInput(), CTX)
    assert not run.success and "timed out after 0.05s" in (run.error or "")


# -------------------------------------------------------------- end to end --


async def test_agent_analyses_demo_data_and_saves_report(ctx, llm) -> None:  # type: ignore[no-untyped-def]
    """Full lifecycle: plan -> csv_analyzer -> report_generator -> review -> COMPLETED."""
    from app.agent.evaluator import GoalEvaluation, StepEvaluation
    from app.agent.executor import ToolArguments
    from app.agent.planner import PlannerOutput
    from app.agent.state import TaskStatus
    from tests.conftest import args, goal_done, plan, step_ok

    demo_dir = ctx.settings.workspace_dir / "demo"
    demo_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(DEMO, demo_dir / "demo_sales.csv")

    llm.add(
        PlannerOutput,
        plan(
            ("Total gross profit by branch", "csv_analyzer", False),
            ("Save the branch report", "report_generator", False),
        ),
    )

    def report_args(prompt: str) -> ToolArguments:
        # The executor passes earlier step results to the LLM; use them in the report.
        assert "sum_gross_profit" in prompt
        return args(
            '{"title": "Gross profit by branch", "filename": "branch_profit",'
            ' "sections": [{"heading": "Ranking", "body": "See table."}]}'
        )

    llm.add(
        ToolArguments,
        args(
            '{"path": "demo/demo_sales.csv", "operation": "aggregate", "group_by": ["branch"],'
            ' "metrics": [{"column": "gross_profit", "agg": "sum"}], "limit": 8}'
        ),
        report_args,
    )
    llm.add(StepEvaluation, step_ok())
    llm.add(GoalEvaluation, goal_done("Report saved to reports/branch_profit.md"))

    task = ctx.tasks.create_task("Rank branches by gross profit and save a report")
    assert await ctx.agent.run(task.id) is TaskStatus.COMPLETED
    calls = ctx.tasks.list_tool_calls(task.id)
    assert [c.tool for c in calls] == ["csv_analyzer", "report_generator"]
    assert all(c.status == "SUCCESS" for c in calls)
    assert (ctx.settings.workspace_dir / "reports" / "branch_profit.md").is_file()
