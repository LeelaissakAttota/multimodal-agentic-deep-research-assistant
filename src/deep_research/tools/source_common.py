"""Shared helpers for the keyless public-source tools (academic, video, social, images).

Every source tool returns ``SourceSearchOutput``: a list of hits shaped like the
web-search hits (``title``/``url``/``snippet``) plus ``source_type`` and a
``content`` field holding the text worth citing (abstract, transcript, post
body, image caption). That lets the live research agent treat them exactly like
web results and cite them in the report.

All tools take an injected ``httpx.AsyncClient`` so tests run offline against
``httpx.MockTransport``. No API keys are needed or read.
"""

from __future__ import annotations

import re
import time
from html import unescape
from typing import Any

import httpx
from pydantic import Field

from deep_research.tools.definition import ToolInput, ToolOutput
from deep_research.tools.tool import ToolResult

USER_AGENT = "deep-research/0.1 (+https://github.com/; research assistant; keyless public APIs)"
MAX_LIMIT = 10


class SourceSearchInput(ToolInput):
    query: str
    limit: int = 5


class SourceSearchOutput(ToolOutput):
    results: list[dict[str, str]] = Field(default_factory=list)
    provider: str = ""
    errors: dict[str, str] = Field(default_factory=dict)


def elapsed_ms(start: float) -> float:
    return (time.monotonic() - start) * 1000


def fail(tool: str, start: float, message: str, kind: str, **extra: Any) -> ToolResult:
    return ToolResult(success=False, error=message, tool_name=tool, execution_time_ms=elapsed_ms(start),
                      metadata={"failure_kind": kind, "tool_identifier": tool, **extra})


def ok(tool: str, start: float, results: list[dict[str, str]], provider: str,
       errors: dict[str, str] | None = None) -> ToolResult:
    return ToolResult(
        success=True,
        output=SourceSearchOutput(results=results, provider=provider, errors=errors or {}),
        tool_name=tool, execution_time_ms=elapsed_ms(start),
        metadata={"tool_identifier": tool, "provider": provider, "result_count": len(results),
                  "provider_errors": errors or {}},
    )


def read_query(params: dict[str, Any], default_limit: int = 5) -> tuple[str, int]:
    """Validated (query, limit); raises ValueError on a missing query."""
    query = params.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("Parameter 'query' must be a non-empty string.")
    raw = params.get("limit", default_limit)
    limit = raw if isinstance(raw, int) and not isinstance(raw, bool) else default_limit
    return " ".join(query.split())[:300], max(1, min(limit, MAX_LIMIT))


def clean(text: Any, max_len: int = 4_000) -> str:
    """Collapse whitespace, strip tags/entities, cap length."""
    value = unescape(re.sub(r"<[^>]+>", " ", str(text or "")))
    return " ".join(value.split())[:max_len]


def error_kind(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"http_{exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return "network_error"
    return "malformed_response"


def hit(*, title: str, url: str, snippet: str, content: str, source_type: str, **extra: Any) -> dict[str, str]:
    row = {"title": clean(title, 300) or url, "url": url, "snippet": clean(snippet, 400),
           "content": clean(content), "source_type": source_type}
    row.update({k: clean(v, 300) for k, v in extra.items() if v not in (None, "", [])})
    return row


async def get_with_backoff(client: httpx.AsyncClient, url: str, *, params: dict[str, Any] | None = None,
                           headers: dict[str, str] | None = None, timeout: float = 15.0,
                           retries: int = 2, base_delay: float = 1.0) -> httpx.Response:
    """GET with retry/backoff on HTTP 429 and 503 (honours a short ``Retry-After``); raises on final failure."""
    import asyncio

    for attempt in range(retries + 1):
        response = await client.get(url, params=params, headers=headers, timeout=timeout)
        if response.status_code not in (429, 503) or attempt == retries:
            response.raise_for_status()
            return response
        retry_after = response.headers.get("Retry-After", "")
        delay = float(retry_after) if retry_after.isdigit() else base_delay * (2 ** attempt)
        if base_delay > 0:
            await asyncio.sleep(min(delay, 8.0))
    raise AssertionError("unreachable")  # pragma: no cover


_STOP = frozenset(
    "a an and are as at be by can does do for from has have how in into is it its of on or say says should than that the "
    "their there these this those to was were what when where which while who why will with about recent latest new "
    "research researchers study studies academic paper papers evidence review show shows effect effects role impact "
    "impacts between vs versus explain explained tell me".split())


def query_terms(query: str) -> list[str]:
    """Content words of a query (lower-cased, stop words removed, de-duplicated)."""
    words = re.findall(r"[a-z0-9]+", query.lower())
    return list(dict.fromkeys(w for w in words if len(w) > 2 and w not in _STOP))


def relevance(query: str, text: str) -> float:
    """Share of the query's content words found in ``text`` (prefix match tolerates plurals/stems)."""
    terms = query_terms(query)
    if not terms:
        return 1.0
    words = set(re.findall(r"[a-z0-9]+", text.lower()))
    stems = {w[:5] for w in words}
    found = sum(1 for t in terms if t in words or t[:5] in stems)
    return found / len(terms)


def filter_relevant(query: str, rows: list[dict[str, str]], threshold: float = 0.34) -> list[dict[str, str]]:
    """Drop rows whose title+abstract barely match the query, then sort best-first (stable)."""
    scored = [(relevance(query, f"{r.get('title', '')} {r.get('content') or r.get('snippet', '')}"), i, r)
              for i, r in enumerate(rows)]
    kept = [(s, i, r) for s, i, r in scored if s >= threshold]
    kept.sort(key=lambda t: (-t[0], t[1]))
    return [{**r, "relevance": f"{s:.2f}"} for s, _, r in kept]
