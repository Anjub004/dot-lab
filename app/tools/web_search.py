"""Web search tool with pluggable providers.

Dot Lab does not ship its own search index. Configure a provider with
``SEARCH_PROVIDER`` and ``SEARCH_API_KEY``:

* ``tavily`` — https://tavily.com (``POST https://api.tavily.com/search``)
* ``brave``  — https://brave.com/search/api (``GET https://api.search.brave.com/res/v1/web/search``)
* ``none``   — search disabled; the tool reports itself unavailable and the
  planner will not offer it to the model.

Add another provider by subclassing :class:`SearchProvider`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import httpx
from pydantic import BaseModel, Field

from app.tools.base import (
    BaseTool,
    ToolAvailability,
    ToolConfigurationError,
    ToolContext,
    ToolError,
    ToolResult,
)


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str = ""


class SearchProvider(ABC):
    """Interface for search back-ends."""

    name: str = "abstract"

    @abstractmethod
    async def search(self, query: str, max_results: int) -> list[SearchResult]: ...

    def availability(self) -> ToolAvailability:
        return ToolAvailability(available=True)


class UnconfiguredSearchProvider(SearchProvider):
    """Used when no provider is configured. Fails with an actionable message."""

    name = "none"
    MESSAGE = (
        "Web search is not configured. Set SEARCH_PROVIDER to 'tavily' or 'brave' "
        "and SEARCH_API_KEY to your provider API key in .env, then restart Dot Lab."
    )

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        raise ToolConfigurationError(self.MESSAGE)

    def availability(self) -> ToolAvailability:
        return ToolAvailability(available=False, reason=self.MESSAGE)


class _HttpSearchProvider(SearchProvider):
    def __init__(
        self, api_key: str, timeout: float = 20.0, client: httpx.AsyncClient | None = None
    ):
        if not api_key:
            raise ToolConfigurationError(f"SEARCH_API_KEY is required for provider '{self.name}'")
        self._api_key = api_key
        self._timeout = timeout
        self._client = client

    async def _request(self, method: str, url: str, **kwargs: object) -> dict:
        try:
            if self._client is not None:
                response = await self._client.request(method, url, **kwargs)  # type: ignore[arg-type]
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(method, url, **kwargs)  # type: ignore[arg-type]
        except httpx.TimeoutException as exc:
            raise ToolError(f"{self.name} search timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ToolError(
                f"{self.name} search network error: {type(exc).__name__}", retryable=True
            ) from exc

        if response.status_code in (401, 403):
            raise ToolConfigurationError(
                f"{self.name} rejected the API key (HTTP {response.status_code})"
            )
        if response.status_code == 429 or response.status_code >= 500:
            raise ToolError(
                f"{self.name} search unavailable (HTTP {response.status_code})", retryable=True
            )
        if response.status_code >= 400:
            raise ToolError(f"{self.name} search failed (HTTP {response.status_code})")
        try:
            return response.json()
        except ValueError as exc:
            raise ToolError(f"{self.name} returned invalid JSON") from exc


class TavilySearchProvider(_HttpSearchProvider):
    name = "tavily"
    URL = "https://api.tavily.com/search"

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        payload = await self._request(
            "POST",
            self.URL,
            json={"query": query, "max_results": max_results},
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        return [
            SearchResult(
                title=str(item.get("title") or ""),
                url=str(item.get("url") or ""),
                snippet=str(item.get("content") or ""),
            )
            for item in payload.get("results", [])
            if item.get("url")
        ][:max_results]


class BraveSearchProvider(_HttpSearchProvider):
    name = "brave"
    URL = "https://api.search.brave.com/res/v1/web/search"

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        payload = await self._request(
            "GET",
            self.URL,
            params={"q": query, "count": max_results},
            headers={"X-Subscription-Token": self._api_key, "Accept": "application/json"},
        )
        results = payload.get("web", {}).get("results", [])
        return [
            SearchResult(
                title=str(item.get("title") or ""),
                url=str(item.get("url") or ""),
                snippet=str(item.get("description") or ""),
            )
            for item in results
            if item.get("url")
        ][:max_results]


def build_search_provider(provider: str, api_key: str | None) -> SearchProvider:
    """Create the configured provider, or an unconfigured placeholder."""
    if provider == "tavily" and api_key:
        return TavilySearchProvider(api_key)
    if provider == "brave" and api_key:
        return BraveSearchProvider(api_key)
    return UnconfiguredSearchProvider()


class WebSearchInput(BaseModel):
    query: str = Field(min_length=2, max_length=400, description="Search query.")
    max_results: int = Field(default=5, ge=1, le=10, description="Number of results (1-10).")


class WebSearchTool(BaseTool):
    name = "web_search"
    description = (
        "Search the web. Returns titles, URLs and snippets. Use the browser tool "
        "to read a result page in full."
    )
    input_model = WebSearchInput

    def __init__(self, provider: SearchProvider, default_max_results: int = 5) -> None:
        self.provider = provider
        self.default_max_results = default_max_results

    def availability(self) -> ToolAvailability:
        return self.provider.availability()

    async def execute(self, args: BaseModel, context: ToolContext) -> ToolResult:
        params = self.typed(args, WebSearchInput)
        limit = min(params.max_results, self.default_max_results)
        results = await self.provider.search(params.query, limit)
        if not results:
            return ToolResult(content=f"No results for '{params.query}'.", data={"results": []})
        lines = [
            f"{i}. {r.title}\n   {r.url}\n   {r.snippet[:400]}" for i, r in enumerate(results, 1)
        ]
        return ToolResult(
            content="\n".join(lines),
            data={"query": params.query, "results": [r.model_dump() for r in results]},
        )
