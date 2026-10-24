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
