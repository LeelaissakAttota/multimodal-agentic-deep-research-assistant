"""Live web search and page-fetch tools (used only when RESEARCH_MODE=live).

* ``WebSearchTool``: automatic free search chain (DuckDuckGo HTML → Lite →
  Wikipedia → DuckDuckGo Instant Answer), with optional Tavily when a key is
  configured. HTTP 202 / bot-challenge pages are treated as failure and the
  next source is tried.
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
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

import httpx
from pydantic import Field, SecretStr

from deep_research.domain.modality import Modality
from deep_research.tools.definition import ToolCapability, ToolDefinition, ToolInput, ToolOutput
from deep_research.tools.tool import Tool, ToolRequest, ToolResult

WEB_SEARCH_TOOL_ID = "live_web_search"
WEB_FETCH_TOOL_ID = "live_web_fetch"
# Wikimedia (and others) reject generic browser-like agents; send a descriptive one with a URL.
_USER_AGENT = "deep-research/0.1 (+https://github.com/; research assistant; keyless public APIs)"


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
# Web search (automatic free-source chain)
# ---------------------------------------------------------------------------

_DDG_HTML_URL = "https://html.duckduckgo.com/html/"
_DDG_LITE_URL = "https://lite.duckduckgo.com/lite/"
_DDG_INSTANT_URL = "https://api.duckduckgo.com/"
_WIKI_API_URL = "https://en.wikipedia.org/w/api.php"
_TAVILY_URL = "https://api.tavily.com/search"
_CHALLENGE_MARKERS = (
    "anomaly-modal",
    "challenge-form",
    "bot challenge",
    "unusual traffic",
    "captcha",
    "please enable javascript and cookies",
    "duckduckgo.com/anomaly",
)


class WebSearchInput(ToolInput):
    query: str
    limit: int = 5


class WebSearchOutput(ToolOutput):
    results: list[dict[str, str]] = Field(default_factory=list)
    provider: str = ""


class _DDGLiteParser(HTMLParser):
    """Extract (title, url) pairs from DuckDuckGo Lite HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._capture = False
        self._href = ""
        self._title = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if tag == "a" and ("result-link" in classes or a.get("rel") == "nofollow"):
            href = a.get("href") or ""
            if href:
                self._href = _ddg_target(href.replace("&amp;", "&"))
                self._title = ""
                self._capture = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._capture:
            title = " ".join(self._title.split())
            if self._href.startswith(("http://", "https://")) and title:
                self.results.append({"title": title, "url": self._href, "snippet": ""})
            self._capture = False

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._title += data


def _looks_like_challenge(status: int, body: str) -> bool:
    if status == 202:
        return True
    lowered = body[:8000].casefold()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)


def _normalize_hits(rows: list[dict[str, str]], limit: int) -> list[dict[str, str]]:
    cleaned: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        url = (row.get("url") or "").strip()
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        cleaned.append(
            {
                "title": " ".join((row.get("title") or "").split()),
                "url": url,
                "snippet": " ".join((row.get("snippet") or "").split()),
            }
        )
        if len(cleaned) >= limit:
            break
    return cleaned


class WebSearchTool(Tool):
    """Automatic free web-search chain with optional Tavily.

    Default ``provider="auto"`` tries free sources in order and skips blocked
    ones (HTTP 202 / challenge pages / empty / network errors) without config:

    1. DuckDuckGo HTML
    2. DuckDuckGo Lite
    3. Wikipedia full-text search API
    4. DuckDuckGo Instant Answer API

    When ``DEEP_RESEARCH_TAVILY_API_KEY`` is set, Tavily is tried first. Explicit
    ``provider="tavily"`` uses Tavily only; ``provider="duckduckgo"`` uses the
    free chain only.
    """

    def __init__(
        self,
        *,
        provider: str = "auto",
        tavily_api_key: SecretStr | None = None,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
        max_results: int = 10,
    ) -> None:
        if provider not in ("auto", "duckduckgo", "tavily"):
            raise ValueError(f"Unsupported search provider: {provider}")
        has_tavily = bool(tavily_api_key and tavily_api_key.get_secret_value().strip())
        if provider == "tavily" and not has_tavily:
            raise ValueError("Tavily search requires DEEP_RESEARCH_TAVILY_API_KEY")
        self.provider = provider
        self._tavily_key = tavily_api_key if has_tavily else None
        self._client = http_client
        self._timeout = timeout
        self._max_results = max_results
        self.last_provider: str = ""

    @property
    def name(self) -> str:
        return WEB_SEARCH_TOOL_ID

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            identifier=WEB_SEARCH_TOOL_ID,
            name="Live Web Search",
            description="Search the public web via an automatic free-source chain.",
            modality=Modality.WEB,
            capabilities=[ToolCapability.SEARCH],
            input_schema=WebSearchInput,
            output_schema=WebSearchOutput,
            cost_class="FREE",
            network_required=True,
            auth_required=self.provider == "tavily",
            reliability="MEDIUM",
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
            results, used = await self._search_chain(client, query.strip(), limit)
        except httpx.TimeoutException:
            return _fail(WEB_SEARCH_TOOL_ID, start, "Web search timed out.", "timeout")
        except httpx.HTTPStatusError as exc:
            return _fail(
                WEB_SEARCH_TOOL_ID,
                start,
                f"Web search failed (HTTP {exc.response.status_code}).",
                "http_error",
            )
        except httpx.HTTPError:
            return _fail(WEB_SEARCH_TOOL_ID, start, "Web search network error.", "network_error")
        except (ValueError, KeyError, TypeError):
            return _fail(WEB_SEARCH_TOOL_ID, start, "Web search returned a malformed response.", "malformed_response")
        finally:
            if self._client is None:
                await client.aclose()
        self.last_provider = used
        return ToolResult(
            success=True,
            output=WebSearchOutput(results=results, provider=used or self.provider),
            tool_name=WEB_SEARCH_TOOL_ID,
            execution_time_ms=_elapsed(start),
            metadata={
                "tool_identifier": WEB_SEARCH_TOOL_ID,
                "provider": used or self.provider,
                "result_count": len(results),
            },
        )

    async def _search_chain(
        self, client: httpx.AsyncClient, query: str, limit: int
    ) -> tuple[list[dict[str, str]], str]:
        backends: list[tuple[str, Any]] = []
        if self.provider == "tavily":
            backends.append(("tavily", self._tavily))
        elif self.provider == "duckduckgo":
            backends.extend(
                [
                    ("duckduckgo_html", self._ddg_html),
                    ("duckduckgo_lite", self._ddg_lite),
                    ("wikipedia", self._wikipedia),
                    ("duckduckgo_instant", self._ddg_instant),
                ]
            )
        else:  # auto
            if self._tavily_key is not None:
                backends.append(("tavily", self._tavily))
            backends.extend(
                [
                    ("duckduckgo_html", self._ddg_html),
                    ("duckduckgo_lite", self._ddg_lite),
                    ("wikipedia", self._wikipedia),
                    ("duckduckgo_instant", self._ddg_instant),
                ]
            )
        last_error: Exception | None = None
        failures = 0
        for name, fn in backends:
            try:
                hits = await fn(client, query, limit)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                last_error = exc
                failures += 1
                continue
            if hits:
                return hits, name
        # Every backend raised (none merely returned empty): surface the error.
        if last_error is not None and failures == len(backends):
            raise last_error
        return [], ""

    async def _ddg_html(self, client: httpx.AsyncClient, query: str, limit: int) -> list[dict[str, str]]:
        response = await client.post(
            _DDG_HTML_URL,
            data={"q": query},
            headers={"User-Agent": _USER_AGENT},
            timeout=self._timeout,
        )
        if _looks_like_challenge(response.status_code, response.text):
            return []
        response.raise_for_status()
        parser = _DDGParser()
        parser.feed(response.text)
        return _normalize_hits(parser.results, limit)

    async def _ddg_lite(self, client: httpx.AsyncClient, query: str, limit: int) -> list[dict[str, str]]:
        response = await client.get(
            _DDG_LITE_URL,
            params={"q": query},
            headers={"User-Agent": _USER_AGENT},
            timeout=self._timeout,
        )
        if _looks_like_challenge(response.status_code, response.text):
            return []
        response.raise_for_status()
        parser = _DDGLiteParser()
        parser.feed(response.text)
        return _normalize_hits(parser.results, limit)

    async def _wikipedia(self, client: httpx.AsyncClient, query: str, limit: int) -> list[dict[str, str]]:
        """Wikipedia full-text search (works for natural-language questions)."""
        response = await client.get(
            _WIKI_API_URL,
            params={
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": str(limit),
                "srnamespace": "0",
                "format": "json",
            },
            headers={"User-Agent": _USER_AGENT},
            timeout=self._timeout,
        )
        response.raise_for_status()
        data: Any = response.json()
        hits = data.get("query", {}).get("search", []) if isinstance(data, dict) else []
        rows: list[dict[str, str]] = []
        for hit in hits if isinstance(hits, list) else []:
            title = hit.get("title") if isinstance(hit, dict) else None
            if not isinstance(title, str) or not title.strip():
                continue
            _, snippet = html_to_text(str(hit.get("snippet") or ""))
            rows.append(
                {
                    "title": title,
                    "url": "https://en.wikipedia.org/wiki/" + quote(title.replace(" ", "_")),
                    "snippet": snippet,
                }
            )
        return _normalize_hits(rows, limit)

    async def _ddg_instant(self, client: httpx.AsyncClient, query: str, limit: int) -> list[dict[str, str]]:
        response = await client.get(
            _DDG_INSTANT_URL,
            params={"q": query, "format": "json", "no_redirect": "1", "no_html": "1"},
            headers={"User-Agent": _USER_AGENT},
            timeout=self._timeout,
        )
        response.raise_for_status()
        data: Any = response.json()
        rows: list[dict[str, str]] = []
        abstract_url = data.get("AbstractURL") if isinstance(data, dict) else None
        abstract = data.get("Abstract") if isinstance(data, dict) else None
        heading = data.get("Heading") if isinstance(data, dict) else None
        if isinstance(abstract_url, str) and abstract_url.startswith("http"):
            rows.append(
                {
                    "title": str(heading or abstract_url),
                    "url": abstract_url,
                    "snippet": str(abstract or ""),
                }
            )
        related = data.get("RelatedTopics") if isinstance(data, dict) else None
        if isinstance(related, list):
            for item in related:
                rows.extend(_flatten_ddg_topics(item))
        return _normalize_hits(rows, limit)

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
        return _normalize_hits(
            [
                {
                    "title": str(r.get("title", "")),
                    "url": str(r["url"]),
                    "snippet": str(r.get("content", "")),
                }
                for r in data["results"][:limit]
            ],
            limit,
        )


def _flatten_ddg_topics(item: Any) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if not isinstance(item, dict):
        return rows
    if isinstance(item.get("Topics"), list):
        for nested in item["Topics"]:
            rows.extend(_flatten_ddg_topics(nested))
        return rows
    url = item.get("FirstURL")
    text = item.get("Text")
    if isinstance(url, str) and url.startswith("http"):
        rows.append({"title": str(text or url), "url": url, "snippet": str(text or "")})
    return rows


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
