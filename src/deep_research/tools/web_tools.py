"""Live web search and page-fetch tools (used only when RESEARCH_MODE=live).

* ``WebSearchTool``: DuckDuckGo HTML endpoint (free, no key) by default, or
  Tavily when a key is configured.
* ``WebFetchTool``: fetches a public http(s) page and returns plain text.
  Private/loopback/link-local hosts are refused (SSRF guard), redirects are
  re-validated, and the body is size-capped.

Both tools accept an injected ``httpx.AsyncClient`` so tests run offline
against ``httpx.MockTransport``.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
from pydantic import Field, SecretStr

from deep_research.domain.modality import Modality
from deep_research.tools.definition import ToolCapability, ToolDefinition, ToolInput, ToolOutput
from deep_research.tools.tool import Tool, ToolRequest, ToolResult

WEB_SEARCH_TOOL_ID = "live_web_search"
WEB_FETCH_TOOL_ID = "live_web_fetch"
_USER_AGENT = "Mozilla/5.0 (compatible; MADRA-research/0.1)"
_DDG_URL = "https://html.duckduckgo.com/html/"
_TAVILY_URL = "https://api.tavily.com/search"


def _elapsed(start: float) -> float:
    return (time.monotonic() - start) * 1000


def _fail(tool: str, start: float, message: str, kind: str) -> ToolResult:
    return ToolResult(success=False, error=message, tool_name=tool, execution_time_ms=_elapsed(start),
                      metadata={"failure_kind": kind, "tool_identifier": tool})


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------


class _DDGParser(HTMLParser):
    """Extract (title, url, snippet) from DuckDuckGo's HTML results page."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._field: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if tag == "a" and "result__a" in classes:
            self.results.append({"title": "", "url": _ddg_target(a.get("href") or ""), "snippet": ""})
            self._field = "title"
        elif "result__snippet" in classes and self.results:
            self._field = "snippet"

    def handle_endtag(self, tag: str) -> None:
        if tag in ("a", "td", "div"):
            self._field = None

    def handle_data(self, data: str) -> None:
        if self._field and self.results:
            self.results[-1][self._field] += data


def _ddg_target(href: str) -> str:
    """Unwrap DuckDuckGo redirect links (``//duckduckgo.com/l/?uddg=...``)."""
    parsed = urlparse(href if "//" in href else urljoin("https://duckduckgo.com", href))
    if parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg")
        if target:
            return unquote(target[0])
    return href


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg", "head", "template"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        if tag in self._SKIP:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in self._SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip and data.strip():
            self.parts.append(data.strip())


def html_to_text(html: str) -> tuple[str, str]:
    """Return (title, visible text) for an HTML document."""
    parser = _TextExtractor()
    parser.feed(html)
    return parser.title.strip(), " ".join(" ".join(parser.parts).split())


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------


def _resolve(host: str) -> list[str]:
    return [str(info[4][0]) for info in socket.getaddrinfo(host, None)]


def is_public_http_url(url: str, resolver: Any = _resolve) -> bool:
    """True only for http(s) URLs whose host resolves exclusively to public IPs."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    host = parsed.hostname
    try:
        addresses = [host] if _is_ip(host) else resolver(host)
    except OSError:
        return False
    if not addresses:
        return False
    for addr in addresses:
        ip = ipaddress.ip_address(addr)
        if not ip.is_global or ip.is_multicast:
            return False
    return True


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Web search
# ---------------------------------------------------------------------------


class WebSearchInput(ToolInput):
    query: str
    limit: int = 5


class WebSearchOutput(ToolOutput):
    results: list[dict[str, str]] = Field(default_factory=list)
    provider: str = ""


class WebSearchTool(Tool):
    """Free web search (DuckDuckGo HTML) with optional Tavily backend."""

    def __init__(
        self,
        *,
        provider: str = "duckduckgo",
        tavily_api_key: SecretStr | None = None,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
        max_results: int = 10,
    ) -> None:
        if provider not in ("duckduckgo", "tavily"):
            raise ValueError(f"Unsupported search provider: {provider}")
        if provider == "tavily" and not (tavily_api_key and tavily_api_key.get_secret_value().strip()):
            raise ValueError("Tavily search requires MADRA_TAVILY_API_KEY")
        self.provider = provider
        self._tavily_key = tavily_api_key
        self._client = http_client
        self._timeout = timeout
        self._max_results = max_results

    @property
    def name(self) -> str:
        return WEB_SEARCH_TOOL_ID

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            identifier=WEB_SEARCH_TOOL_ID, name="Live Web Search", description="Search the public web.",
            modality=Modality.WEB, capabilities=[ToolCapability.SEARCH], input_schema=WebSearchInput,
            output_schema=WebSearchOutput, cost_class="FREE", network_required=True,
            auth_required=self.provider == "tavily", reliability="MEDIUM",
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, request: ToolRequest) -> ToolResult:
        start = time.monotonic()
        query = request.parameters.get("query")
        if not isinstance(query, str) or not query.strip():
            return _fail(WEB_SEARCH_TOOL_ID, start, "Parameter 'query' must be a non-empty string.", "invalid_input")
        raw_limit = request.parameters.get("limit", 5)
        limit = max(1, min(int(raw_limit) if isinstance(raw_limit, int) else 5, self._max_results))
        client = self._client or httpx.AsyncClient(follow_redirects=True)
        try:
            if self.provider == "tavily":
                results = await self._tavily(client, query.strip(), limit)
            else:
                results = await self._ddg(client, query.strip(), limit)
        except httpx.TimeoutException:
            return _fail(WEB_SEARCH_TOOL_ID, start, "Web search timed out.", "timeout")
        except httpx.HTTPStatusError as exc:
            return _fail(WEB_SEARCH_TOOL_ID, start, f"Web search failed (HTTP {exc.response.status_code}).", "http_error")
        except httpx.HTTPError:
            return _fail(WEB_SEARCH_TOOL_ID, start, "Web search network error.", "network_error")
        except (ValueError, KeyError, TypeError):
            return _fail(WEB_SEARCH_TOOL_ID, start, "Web search returned a malformed response.", "malformed_response")
        finally:
            if self._client is None:
                await client.aclose()
        return ToolResult(
            success=True, output=WebSearchOutput(results=results, provider=self.provider),
            tool_name=WEB_SEARCH_TOOL_ID, execution_time_ms=_elapsed(start),
            metadata={"tool_identifier": WEB_SEARCH_TOOL_ID, "provider": self.provider, "result_count": len(results)},
        )

    async def _ddg(self, client: httpx.AsyncClient, query: str, limit: int) -> list[dict[str, str]]:
        response = await client.post(_DDG_URL, data={"q": query}, headers={"User-Agent": _USER_AGENT},
                                     timeout=self._timeout)
        response.raise_for_status()
        parser = _DDGParser()
        parser.feed(response.text)
        cleaned = [
            {"title": " ".join(r["title"].split()), "url": r["url"], "snippet": " ".join(r["snippet"].split())}
            for r in parser.results
            if r["url"].startswith(("http://", "https://"))
        ]
        return cleaned[:limit]

    async def _tavily(self, client: httpx.AsyncClient, query: str, limit: int) -> list[dict[str, str]]:
        assert self._tavily_key is not None
        response = await client.post(
            _TAVILY_URL,
            headers={"Authorization": f"Bearer {self._tavily_key.get_secret_value()}"},
            json={"query": query, "max_results": limit},
            timeout=self._timeout,
        )
        response.raise_for_status()
        data: Any = response.json()
        return [
            {"title": str(r.get("title", "")), "url": str(r["url"]), "snippet": str(r.get("content", ""))}
            for r in data["results"][:limit]
        ]


# ---------------------------------------------------------------------------
# Web fetch
# ---------------------------------------------------------------------------


class WebFetchInput(ToolInput):
    url: str


class WebFetchOutput(ToolOutput):
    url: str
    title: str = ""
    text: str = ""
    truncated: bool = False


class WebFetchTool(Tool):
    """Fetch a public web page and return its visible text."""

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
        max_bytes: int = 2_000_000,
        max_chars: int = 20_000,
        max_redirects: int = 5,
        url_validator: Any = is_public_http_url,
    ) -> None:
        self._client = http_client
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._max_chars = max_chars
        self._max_redirects = max_redirects
        self._validate = url_validator

    @property
    def name(self) -> str:
        return WEB_FETCH_TOOL_ID

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            identifier=WEB_FETCH_TOOL_ID, name="Live Web Fetch", description="Fetch a public web page as text.",
            modality=Modality.WEB, capabilities=[ToolCapability.READ], input_schema=WebFetchInput,
            output_schema=WebFetchOutput, cost_class="FREE", network_required=True, reliability="MEDIUM",
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, request: ToolRequest) -> ToolResult:
        start = time.monotonic()
        url = request.parameters.get("url")
        if not isinstance(url, str) or not self._validate(url):
            return _fail(WEB_FETCH_TOOL_ID, start, "URL must be a public http(s) address.", "blocked_url")
        client = self._client or httpx.AsyncClient()
        try:
            downloaded = await self._download(client, url, start)
        except httpx.TimeoutException:
            return _fail(WEB_FETCH_TOOL_ID, start, "Fetch timed out.", "timeout")
        except httpx.HTTPError:
            return _fail(WEB_FETCH_TOOL_ID, start, "Fetch network error.", "network_error")
        finally:
            if self._client is None:
                await client.aclose()
        if isinstance(downloaded, ToolResult):
            return downloaded
        url, raw = downloaded
        title, text = html_to_text(raw) if "<" in raw else ("", " ".join(raw.split()))
        truncated = len(text) > self._max_chars
        return ToolResult(
            success=True,
            output=WebFetchOutput(url=url, title=title, text=text[: self._max_chars], truncated=truncated),
            tool_name=WEB_FETCH_TOOL_ID, execution_time_ms=_elapsed(start),
            metadata={"tool_identifier": WEB_FETCH_TOOL_ID, "url": url},
        )

    async def _download(self, client: httpx.AsyncClient, url: str, start: float) -> tuple[str, str] | ToolResult:
        """Follow validated redirects; return (final_url, body) or a failure."""
        for _ in range(self._max_redirects + 1):
            async with client.stream("GET", url, headers={"User-Agent": _USER_AGENT}, timeout=self._timeout,
                                     follow_redirects=False) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers.get("location", ""))
                    if not self._validate(url):
                        return _fail(WEB_FETCH_TOOL_ID, start, "Redirect target is not a public address.", "blocked_url")
                    continue
                if response.status_code >= 400:
                    return _fail(WEB_FETCH_TOOL_ID, start, f"Fetch failed (HTTP {response.status_code}).", "http_error")
                ctype = response.headers.get("content-type", "").lower()
                if ctype and not any(t in ctype for t in ("text/html", "text/plain", "xhtml")):
                    return _fail(WEB_FETCH_TOOL_ID, start, f"Unsupported content type: {ctype}", "unsupported_type")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_bytes:
                        return _fail(WEB_FETCH_TOOL_ID, start, "Page exceeds the maximum size.", "too_large")
                return url, body.decode(response.encoding or "utf-8", errors="replace")
        return _fail(WEB_FETCH_TOOL_ID, start, "Too many redirects.", "too_many_redirects")
