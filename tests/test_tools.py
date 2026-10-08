"""Tool tests: registry, calculator, sandboxed filesystem, search, browser, notifications."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.database.database import Database
from app.tools import build_default_registry
from app.tools.base import (
    ToolConfigurationError,
    ToolContext,
    ToolError,
    ToolInputError,
    ToolRegistry,
)
from app.tools.browser import (
    BlockedUrlError,
    BrowserInput,
    BrowserTool,
    HttpxBrowserBackend,
    UrlPolicy,
    extract_page,
)
from app.tools.calculator import CalculatorInput, CalculatorTool, evaluate_expression
from app.tools.filesystem import FileSystemInput, FileSystemTool, PathSecurityError
from app.tools.notifications import (
    NotificationInput,
    NotificationStore,
    NotificationTool,
    WebhookSender,
)
from app.tools.web_search import (
    TavilySearchProvider,
    UnconfiguredSearchProvider,
    WebSearchInput,
    WebSearchTool,
)

CTX = ToolContext(task_id="t1", step_id="step_1")


# ------------------------------------------------------------ registry --
def test_tool_registry(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 't.db'}")
    db.create_all()
    settings = Settings(workspace_dir=tmp_path / "ws", database_url=db.url)
    registry = build_default_registry(settings, NotificationStore(db))

    assert set(registry.names()) == {
        "calculator",
        "web_search",
        "browser",
        "filesystem",
        "notification",
    }
    assert registry.has("calculator")
    with pytest.raises(ToolError, match="Unknown tool"):
        registry.get("shell")
    with pytest.raises(ValueError):
        registry.register(CalculatorTool())
    # No search provider configured -> web_search is reported unavailable, others are available.
    available = {t.name for t in registry.available()}
    assert "web_search" not in available and "calculator" in available
    described = {d["name"]: d for d in (t.describe() for t in registry.all())}
    assert described["web_search"]["available"] is False
    assert "SEARCH_PROVIDER" in described["web_search"]["unavailable_reason"]
    assert described["calculator"]["input_schema"]["properties"]["expression"]


# ---------------------------------------------------------- calculator --
async def test_calculator() -> None:
    tool = CalculatorTool()
    result = await tool.execute(CalculatorInput(expression="(12.5 * 4) / 2 + mean([1, 2, 3])"), CTX)
    assert result.data["result"] == 27.0
    assert evaluate_expression("2 ** 10") == 1024
    assert evaluate_expression("round(sqrt(2), 3)") == 1.414
    assert evaluate_expression("-pi + pi") == 0


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('ls')",
        "open('/etc/passwd')",
        "(1).__class__",
        "9 ** 9 ** 9",
        "x + 1",
        "'a' * 3",
        "lambda: 1",
        "[1, 2]",
    ],
)
def test_calculator_rejects_unsafe_input(expression: str) -> None:
    with pytest.raises(ToolError):
        evaluate_expression(expression)


def test_calculator_math_error() -> None:
    with pytest.raises(ToolError, match="Math error"):
        evaluate_expression("1/0")


# ---------------------------------------------------------- filesystem --
@pytest.fixture
def fs_tool(tmp_path: Path) -> FileSystemTool:
    return FileSystemTool(tmp_path / "workspace", max_bytes=1000)


@pytest.mark.parametrize(
    "path",
    [
        "../secret.txt",
        "../../etc/passwd",
        "notes/../../escape.txt",
        "notes\\..\\..\\escape.txt",
        "/etc/passwd",
        "\\Windows\\system.ini",
        "C:\\Windows\\win.ini",
        "C:relative.txt",
        "~/.ssh/id_rsa",
        "file\x00.txt",
    ],
)
async def test_path_traversal_protection(fs_tool: FileSystemTool, path: str) -> None:
    with pytest.raises(PathSecurityError):
        fs_tool.workspace.resolve(path)
    with pytest.raises(PathSecurityError):
        await fs_tool.execute(FileSystemInput(operation="write", path=path, content="x"), CTX)


async def test_symlink_escape_is_blocked(fs_tool: FileSystemTool, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (fs_tool.workspace.root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathSecurityError):
        fs_tool.workspace.resolve("link/data.txt")


async def test_filesystem_read_write_list(fs_tool: FileSystemTool) -> None:
    await fs_tool.execute(FileSystemInput(operation="write", path="a/b.txt", content="hello"), CTX)
    read = await fs_tool.execute(FileSystemInput(operation="read", path="a/b.txt"), CTX)
    assert read.content == "hello"
    listing = await fs_tool.execute(FileSystemInput(operation="list", path="a"), CTX)
    assert listing.data["entries"][0]["path"] == "a/b.txt"
    root = await fs_tool.execute(FileSystemInput(operation="list"), CTX)
    assert root.data["entries"][0] == {"path": "a", "type": "dir", "bytes": None}

    with pytest.raises(ToolError, match="not found"):
        await fs_tool.execute(FileSystemInput(operation="read", path="missing.txt"), CTX)
    with pytest.raises(ToolError, match="limit"):
        await fs_tool.execute(
            FileSystemInput(operation="write", path="big.txt", content="x" * 2000), CTX
        )


async def test_filesystem_overwrite_requires_approval(fs_tool: FileSystemTool) -> None:
    new_file = FileSystemInput(operation="write", path="r.md", content="v1")
    assert fs_tool.approval_reason(new_file) is None
    await fs_tool.execute(new_file, CTX)
    assert "overwrite" in (fs_tool.approval_reason(new_file) or "")
    assert fs_tool.approval_reason(FileSystemInput(operation="read", path="r.md")) is None


def test_filesystem_input_validation(fs_tool: FileSystemTool) -> None:
    with pytest.raises(ToolInputError):
        fs_tool.parse_input({"operation": "write", "path": "x.txt"})  # missing content
    with pytest.raises(ToolInputError):
        fs_tool.parse_input({"operation": "delete", "path": "x.txt"})


# ---------------------------------------------------------- web search --
async def test_web_search_unconfigured_fails_gracefully() -> None:
    tool = WebSearchTool(UnconfiguredSearchProvider())
    assert tool.availability().available is False
    with pytest.raises(ToolConfigurationError, match="SEARCH_PROVIDER"):
        await tool.execute(WebSearchInput(query="ai agents"), CTX)


async def test_tavily_provider_with_mock_transport() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "Agents 2026", "url": "https://example.org/a", "content": "News"},
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tool = WebSearchTool(TavilySearchProvider("tvly-test-key", client=client))
    result = await tool.execute(WebSearchInput(query="ai agents", max_results=3), CTX)
    await client.aclose()

    assert seen["auth"] == "Bearer tvly-test-key"
    assert seen["body"]["query"] == "ai agents"
    assert result.data["results"][0]["url"] == "https://example.org/a"
    assert "Agents 2026" in result.content


async def test_search_provider_errors_are_classified() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    provider = TavilySearchProvider("key", client=client)
    with pytest.raises(ToolError) as info:
        await provider.search("x", 3)
    assert info.value.retryable
    await client.aclose()


# ------------------------------------------------------------- browser --
async def test_browser_blocks_unsafe_urls() -> None:
    policy = UrlPolicy(allowed_domains=[], allow_private_networks=False)
    for url in [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "http://127.0.0.1:8000/admin",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.5/",
        "http://[::1]/",
        "http://user:pass@93.184.216.34/",
    ]:
        with pytest.raises(BlockedUrlError):
            await policy.check(url)


async def test_browser_domain_allowlist() -> None:
    policy = UrlPolicy(allowed_domains=["example.org"], allow_private_networks=True)
    await policy.check("https://docs.example.org/page")
    with pytest.raises(BlockedUrlError, match="BROWSER_ALLOWED_DOMAINS"):
        await policy.check("https://evil.test/page")


async def test_browser_reads_text_and_links_with_mock_transport() -> None:
    html = """<html><head><title>Agent News</title><style>.x{}</style></head>
      <body><h1>Headline</h1><script>alert(1)</script><p>Agents are improving.</p>
      <a href="/more">Read more</a></body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html=html)

    policy = UrlPolicy(allowed_domains=[], allow_private_networks=False)
    backend = HttpxBrowserBackend(
        policy, timeout=5, max_bytes=100_000, transport=httpx.MockTransport(handler)
    )
    tool = BrowserTool(backend, max_chars=10_000)
    url = "http://93.184.216.34/news"  # public IP literal: no DNS needed offline

    page = await tool.execute(BrowserInput(url=url), CTX)
    assert "Agents are improving." in page.content and "alert(1)" not in page.content
    assert page.data["title"] == "Agent News"

    links = await tool.execute(BrowserInput(url=url, action="extract_links"), CTX)
    assert links.data["links"] == [{"url": "http://93.184.216.34/more", "text": "Read more"}]


async def test_browser_rechecks_redirect_targets() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/internal"})

    policy = UrlPolicy(allowed_domains=[], allow_private_networks=False)
    backend = HttpxBrowserBackend(policy, 5, 100_000, transport=httpx.MockTransport(handler))
    with pytest.raises(BlockedUrlError):
        await backend.fetch("http://93.184.216.34/redirect")


def test_extract_page_handles_entities() -> None:
    page = extract_page("<p>Fish &amp; chips</p>", "https://x.test/")
    assert page.text == "Fish & chips"


# ------------------------------------------------------- notifications --
async def test_notification_channels(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'n.db'}")
    db.create_all()
    store = NotificationStore(db)
    tool = NotificationTool(store, WebhookSender(None))

    dashboard = NotificationInput(title="Hi", message="Hello")
    assert tool.approval_reason(dashboard) is None
    await tool.execute(dashboard, CTX)
    stored = store.list()
    assert stored[0].title == "Hi" and stored[0].task_id == "t1"

    webhook = NotificationInput(title="Hi", message="Hello", channel="webhook")
    assert "external" in (tool.approval_reason(webhook) or "")
    with pytest.raises(ToolConfigurationError, match="NOTIFICATION_WEBHOOK_URL"):
        await tool.execute(webhook, CTX)
    db.dispose()


def test_registry_rejects_duplicates() -> None:
    registry = ToolRegistry([CalculatorTool()])
    with pytest.raises(ValueError):
        registry.register(CalculatorTool())
