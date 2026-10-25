"""Hacker News (Algolia) tool, all network mocked."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

import httpx
import pytest

from deep_research.domain.modality import Modality
from deep_research.tools.hackernews_tool import HACKERNEWS_TOOL_ID, HackerNewsSearchTool, safe_external_url
from deep_research.tools.tool import ToolRequest

STORIES = {"hits": [
    {"objectID": "101", "title": "Retrieval augmented generation in practice", "url": "https://example.com/rag",
     "points": 250, "num_comments": 80, "created_at": "2025-01-02T03:04:05Z", "author": "pg"},
    {"objectID": "102", "title": "Ask HN: retrieval augmented generation stacks?", "url": None,
     "story_text": "<p>What do you use for RAG &amp; retrieval?</p>", "points": 40, "num_comments": 12},
    {"objectID": "103", "title": "Retrieval augmented generation on localhost", "url": "http://127.0.0.1:8080/x",
     "points": 3, "num_comments": 0},
    {"objectID": "104", "title": "Gardening tips for spring", "url": "https://garden.example/", "points": 999},
    {"objectID": "not-a-number", "title": "Retrieval augmented generation bad id"},
    "junk",
]}
COMMENTS = {"hits": [{"comment_text": "<i>Chunking</i> matters most."}, {"comment_text": ""}, "x"]}


def _client(handler: Callable[[httpx.Request], httpx.Response], calls: list[httpx.Request]) -> httpx.AsyncClient:
    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)
    return httpx.AsyncClient(transport=httpx.MockTransport(wrapped))


def _ok(request: httpx.Request) -> httpx.Response:
    tags = request.url.params.get("tags", "")
    return httpx.Response(200, json=COMMENTS if tags.startswith("comment") else STORIES)


def _run(tool: HackerNewsSearchTool, **params: Any) -> Any:
    return asyncio.run(tool.execute(ToolRequest(tool_name=HACKERNEWS_TOOL_ID, parameters=params)))


def test_definition_is_free_keyless_social() -> None:
    d = HackerNewsSearchTool().get_definition()
    assert d.modality == Modality.SOCIAL and d.cost_class == "FREE" and not d.auth_required


def test_search_returns_stories_with_points_comments_and_item_link() -> None:
    calls: list[httpx.Request] = []
    result = _run(HackerNewsSearchTool(http_client=_client(_ok, calls)), query="retrieval augmented generation")
    assert result.success and result.metadata["provider"] == "hn_algolia"
    rows = result.output.results
    assert [r["item_id"] for r in rows] == ["101", "102", "103"]  # off-topic + bad id + junk dropped
    first = rows[0]
    assert first["url"] == first["hn_url"] == "https://news.ycombinator.com/item?id=101"
    assert first["story_url"] == "https://example.com/rag" and "links to https://example.com/rag" in first["content"]
    assert (first["points"], first["num_comments"], first["created_at"]) == ("250", "80", "2025-01-02T03:04:05Z")
    assert first["source_type"] == "social" and first["provider"] == "hackernews"
    assert rows[1]["content"] == "What do you use for RAG & retrieval?"
    assert "story_url" not in rows[2]  # SSRF guard: private IP link dropped
    q = calls[0].url.params
    assert calls[0].url.host == "hn.algolia.com" and q["tags"] == "story" and q["query"] == "retrieval augmented generation"
    assert calls[0].headers["User-Agent"].startswith("deep-research")


def test_comments_are_fetched_for_top_stories() -> None:
    calls: list[httpx.Request] = []
    result = _run(HackerNewsSearchTool(http_client=_client(_ok, calls)), query="retrieval augmented generation",
                  comments=2, limit=1)
    assert "Top comments: Chunking matters most." in result.output.results[0]["content"]
    assert calls[1].url.params["tags"] == "comment,story_101"


def test_retries_on_429_then_succeeds() -> None:
    seen: list[httpx.Request] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429) if len(seen) == 1 else _ok(request)
    result = _run(HackerNewsSearchTool(http_client=_client(flaky, seen)), query="retrieval augmented generation")
    assert result.success and len(seen) == 2


@pytest.mark.parametrize(("status", "kind"), [(429, "rate_limited"), (403, "blocked"), (500, "http_500")])
def test_http_errors_degrade_with_typed_kind_and_log(status: int, kind: str, caplog: Any) -> None:
    with caplog.at_level(logging.WARNING, logger="deep_research.tools.hackernews_tool"):
        result = _run(HackerNewsSearchTool(http_client=_client(lambda r: httpx.Response(status), [])), query="rag")
    assert not result.success and result.metadata["failure_kind"] == kind and result.metadata["http_status"] == status
    assert "falling back to web search" in caplog.text


def test_malformed_and_network_errors_do_not_crash() -> None:
    bad = _run(HackerNewsSearchTool(http_client=_client(lambda r: httpx.Response(200, json=["x"]), [])), query="rag")
    assert bad.metadata["failure_kind"] == "malformed_response"

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)
    assert _run(HackerNewsSearchTool(http_client=_client(boom, [])), query="rag").metadata["failure_kind"] == "timeout"
    assert _run(HackerNewsSearchTool(http_client=_client(_ok, [])), query=" ").metadata["failure_kind"] == "invalid_input"


def test_comment_failure_is_recorded_not_fatal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500) if request.url.params.get("tags", "").startswith("comment") else _ok(request)
    result = _run(HackerNewsSearchTool(http_client=_client(handler, [])), query="retrieval augmented generation",
                  comments=1)
    assert result.success and result.metadata["provider_errors"]["101"] == "http_500"


@pytest.mark.parametrize(("url", "kept"), [
    ("https://example.com/a", True), ("http://news.example.org", True), ("ftp://example.com", False),
    ("http://localhost:8000", False), ("http://10.0.0.1/", False), ("http://169.254.169.254/latest", False),
    ("https://user:pw@example.com", False), ("http://printer.local/", False), ("", False), (None, False),
])
def test_safe_external_url_static_ssrf_checks(url: Any, kept: bool) -> None:
    assert bool(safe_external_url(url)) is kept
