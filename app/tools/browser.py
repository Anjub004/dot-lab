"""Read-only browser tool with safety controls.

SECURITY NOTE: letting an AI agent drive a browser is risky. Pages can contain
prompt-injection text, links to internal services (SSRF), or huge payloads.
This tool is deliberately conservative:

* read-only: it fetches pages and extracts text/links; it never clicks, types,
  submits forms, downloads files, or runs page-provided actions,
* only ``http``/``https`` URLs,
* hosts resolving to private, loopback, link-local or reserved IP addresses are
  blocked unless ``BROWSER_ALLOW_PRIVATE_NETWORKS=true``,
* optional domain allow-list (``BROWSER_ALLOWED_DOMAINS``),
* redirects are followed manually (max 5) and every hop is re-checked,
* response size and extracted text are capped, and only text/HTML is read.

Two backends implement :class:`BrowserBackend`:

* ``httpx`` (default): plain HTTP fetch + HTML text extraction. No JavaScript.
* ``playwright`` (optional): headless Chromium for JavaScript-rendered pages.
  Install with ``pip install playwright && playwright install chromium``.

"Navigation" is modelled as fetching another URL (for example a link returned
by ``extract_links``); the tool keeps no session, cookies, or logins.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from abc import ABC, abstractmethod
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel, Field

from app.tools.base import (
    BaseTool,
    ToolAvailability,
    ToolContext,
    ToolError,
    ToolInputError,
    ToolResult,
)

MAX_REDIRECTS = 5
MAX_LINKS = 100
USER_AGENT = "DotLab/0.1 (+experimental research agent; read-only)"
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "iframe", "head"}
_BLOCK_TAGS = {
    "p",
    "div",
    "br",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "tr",
    "section",
    "article",
}


class BlockedUrlError(ToolInputError):
    """The URL is not allowed by the browser safety policy."""


class PageContent(BaseModel):
    url: str
    title: str = ""
    text: str = ""
    links: list[dict[str, str]] = Field(default_factory=list)


class _TextExtractor(HTMLParser):
    """Collects visible text, the page title, and links from HTML."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title = ""
        self.links: list[dict[str, str]] = []
        self._chunks: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._current_link: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")
        if tag == "a":
            href = dict(attrs).get("href")
            if href and not href.startswith(("javascript:", "mailto:", "#")):
                self._current_link = {"url": urljoin(self.base_url, href), "text": ""}

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        if tag == "a" and self._current_link is not None:
            if len(self.links) < MAX_LINKS:
                self._current_link["text"] = self._current_link["text"].strip()[:200]
                self.links.append(self._current_link)
            self._current_link = None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skip_depth:
            return
        self._chunks.append(data)
        if self._current_link is not None:
            self._current_link["text"] += data

    def text(self) -> str:
        raw = "".join(self._chunks)
        lines = (" ".join(line.split()) for line in raw.splitlines())
        return "\n".join(line for line in lines if line)


def extract_page(html: str, url: str) -> PageContent:
    parser = _TextExtractor(url)
    parser.feed(html)
    parser.close()
    return PageContent(url=url, title=parser.title.strip(), text=parser.text(), links=parser.links)


class UrlPolicy:
    """Decides whether the browser may fetch a URL."""

    def __init__(self, allowed_domains: list[str], allow_private_networks: bool) -> None:
        self.allowed_domains = [d.lower().lstrip(".") for d in allowed_domains]
        self.allow_private_networks = allow_private_networks

    def _domain_allowed(self, host: str) -> bool:
        if not self.allowed_domains:
            return True
        return any(host == d or host.endswith("." + d) for d in self.allowed_domains)

    async def check(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise BlockedUrlError("Only http and https URLs are allowed")
        host = (parts.hostname or "").lower()
        if not host:
            raise BlockedUrlError("URL has no host")
        if parts.username or parts.password:
            raise BlockedUrlError("URLs with embedded credentials are not allowed")
        if not self._domain_allowed(host):
            raise BlockedUrlError(f"Domain '{host}' is not in BROWSER_ALLOWED_DOMAINS")
        if self.allow_private_networks:
            return
        for address in await self._resolve(host, parts.port):
            ip = ipaddress.ip_address(address)
            if not ip.is_global or ip.is_multicast:
                raise BlockedUrlError(f"Host '{host}' resolves to a non-public address")

    @staticmethod
    async def _resolve(host: str, port: int | None) -> list[str]:
        try:
            return [str(ipaddress.ip_address(host))]
        except ValueError:
            pass
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ToolError(f"Could not resolve host '{host}'", retryable=True) from exc
        return sorted({str(info[4][0]) for info in infos})


class BrowserBackend(ABC):
    """Fetches a page and returns its extracted content."""

    @abstractmethod
    async def fetch(self, url: str) -> PageContent: ...


class HttpxBrowserBackend(BrowserBackend):
    """Plain HTTP backend (no JavaScript execution)."""

    def __init__(
        self,
        policy: UrlPolicy,
        timeout: float,
        max_bytes: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.policy = policy
        self.timeout = timeout
        self.max_bytes = max_bytes
        self._transport = transport

    async def fetch(self, url: str) -> PageContent:
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT},
            transport=self._transport,
        ) as client:
            current = url
            for _ in range(MAX_REDIRECTS + 1):
                await self.policy.check(current)
                body, response = await self._get(client, current)
                if response.is_redirect and "location" in response.headers:
                    current = urljoin(current, response.headers["location"])
                    continue
                return self._to_page(current, body, response)
        raise ToolError("Too many redirects")

    async def _get(self, client: httpx.AsyncClient, url: str) -> tuple[bytes, httpx.Response]:
        try:
            async with client.stream("GET", url) as response:
                if response.is_redirect:
                    return b"", response
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self.max_bytes:
                        break  # truncate oversized pages
                    chunks.append(chunk)
                return b"".join(chunks), response
        except httpx.TimeoutException as exc:
            raise ToolError("Page load timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ToolError(f"Network error: {type(exc).__name__}", retryable=True) from exc

    @staticmethod
    def _to_page(url: str, body: bytes, response: httpx.Response) -> PageContent:
        if response.status_code >= 500:
            raise ToolError(f"Server error (HTTP {response.status_code})", retryable=True)
        if response.status_code >= 400:
            raise ToolError(f"Page returned HTTP {response.status_code}")
        content_type = response.headers.get("content-type", "").lower()
        text = body.decode(response.encoding or "utf-8", errors="replace")
        if "html" in content_type or not content_type:
            return extract_page(text, url)
        if content_type.startswith("text/") or "json" in content_type:
            return PageContent(url=url, text=text)
        raise ToolError(f"Unsupported content type '{content_type}' (only text/HTML is read)")


class PlaywrightBrowserBackend(BrowserBackend):
    """Headless Chromium backend for JavaScript-heavy pages (optional dependency).

    Every network request the page makes is checked against the same URL
    policy; disallowed requests are aborted.
    """

    def __init__(self, policy: UrlPolicy, timeout: float) -> None:
        self.policy = policy
        self.timeout = timeout

    async def fetch(self, url: str) -> PageContent:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ToolError(
                "BROWSER_BACKEND=playwright but Playwright is not installed. "
                "Run: pip install playwright && playwright install chromium"
            ) from exc

        await self.policy.check(url)

        async def guard(route, request):  # type: ignore[no-untyped-def]
            try:
                await self.policy.check(request.url)
            except ToolError:
                await route.abort()
                return
            await route.continue_()

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context(user_agent=USER_AGENT, accept_downloads=False)
                page = await context.new_page()
                await page.route("**/*", guard)
                await page.goto(url, timeout=self.timeout * 1000, wait_until="domcontentloaded")
                html = await page.content()
                return extract_page(html, page.url)
            except Exception as exc:  # Playwright raises its own error hierarchy
                raise ToolError(f"Browser error: {type(exc).__name__}", retryable=True) from exc
            finally:
                await browser.close()


class BrowserInput(BaseModel):
    url: str = Field(min_length=8, max_length=2000, description="Absolute http(s) URL to open.")
    action: Literal["read_text", "extract_links"] = Field(
        default="read_text",
        description="'read_text' returns readable page text; 'extract_links' returns its links.",
    )


class BrowserTool(BaseTool):
    name = "browser"
    description = (
        "Open a public web page (read-only) and return its text or its links. "
        "To navigate, call again with a link URL. Cannot log in, click, or submit forms."
    )
    input_model = BrowserInput

    def __init__(self, backend: BrowserBackend, max_chars: int) -> None:
        self.backend = backend
        self.max_chars = max_chars

    def availability(self) -> ToolAvailability:
        return ToolAvailability(available=True)

    async def execute(self, args: BaseModel, context: ToolContext) -> ToolResult:
        params = self.typed(args, BrowserInput)
        page = await self.backend.fetch(params.url)
        if params.action == "extract_links":
            lines = [f"- {link['text'] or '(no text)'}: {link['url']}" for link in page.links]
            return ToolResult(
                content=f"Links on {page.url}:\n" + ("\n".join(lines) or "(none)"),
                data={"url": page.url, "title": page.title, "links": page.links},
            )
        text = page.text[: self.max_chars]
        truncated = len(page.text) > self.max_chars
        header = f"Title: {page.title}\nURL: {page.url}\n\n"
        return ToolResult(
            content=header + text + ("\n[...truncated]" if truncated else ""),
            data={"url": page.url, "title": page.title, "chars": len(text), "truncated": truncated},
        )


def build_browser_backend(
    backend: str, policy: UrlPolicy, timeout: float, max_bytes: int
) -> BrowserBackend:
    if backend == "playwright":
        return PlaywrightBrowserBackend(policy, timeout)
    return HttpxBrowserBackend(policy, timeout, max_bytes)
