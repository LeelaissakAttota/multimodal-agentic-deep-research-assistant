"""Edge cases for Hacker News, Bluesky and Stack Exchange tools (Testing Bot; all network mocked)."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

import httpx
import pytest

from deep_research.core.agents.planning_agent import SOURCE_ROUTES, route_source_tools
from deep_research.tools.bluesky_tool import BLUESKY_TOOL_ID, BlueskySearchTool
from deep_research.tools.hackernews_tool import HACKERNEWS_TOOL_ID, HackerNewsSearchTool
from deep_research.tools.stackexchange_tool import STACKEXCHANGE_TOOL_ID, StackExchangeSearchTool
from deep_research.tools.tool import ToolRequest, ToolResult

Handler = Callable[[httpx.Request], httpx.Response]
TOOLS: dict[str, Any] = {
    HACKERNEWS_TOOL_ID: HackerNewsSearchTool,
    BLUESKY_TOOL_ID: BlueskySearchTool,
    STACKEXCHANGE_TOOL_ID: StackExchangeSearchTool,
}


def _run(tool_id: str, handler: Handler, query: str = "retrieval augmented generation") -> ToolResult:
    async def go() -> ToolResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            tool = TOOLS[tool_id](http_client=client)
            return await tool.execute(ToolRequest(tool_name=tool_id, parameters={"query": query, "limit": 3}))
    return asyncio.run(go())


def _json(body: Any, status: int = 200) -> Handler:
    return lambda _r: httpx.Response(status, json=body)


EMPTY = {HACKERNEWS_TOOL_ID: {"hits": []}, BLUESKY_TOOL_ID: {"posts": []},
         STACKEXCHANGE_TOOL_ID: {"items": [], "quota_remaining": 100}}
NULLS = {HACKERNEWS_TOOL_ID: {"hits": None}, BLUESKY_TOOL_ID: {"posts": None},
         STACKEXCHANGE_TOOL_ID: {"items": None, "quota_remaining": 100}}
MISSING = {HACKERNEWS_TOOL_ID: {"hits": [{}, {"objectID": None, "title": None}]},
           BLUESKY_TOOL_ID: {"posts": [{}, {"uri": None, "author": None, "record": None}]},
           STACKEXCHANGE_TOOL_ID: {"items": [{}, {"title": None, "link": None, "owner": None, "tags": None}],
                                   "quota_remaining": 100}}
ids = list(TOOLS)


@pytest.mark.parametrize("tool_id", ids)
def test_empty_results_no_crash(tool_id: str) -> None:
    r = _run(tool_id, _json(EMPTY[tool_id]))
    assert isinstance(r, ToolResult)
    if r.success:
        assert r.output.results == []


@pytest.mark.parametrize("tool_id", ids)
def test_null_lists_no_crash(tool_id: str) -> None:
    r = _run(tool_id, _json(NULLS[tool_id]))
    assert isinstance(r, ToolResult)
    if r.success:
        assert r.output.results == []


@pytest.mark.parametrize("tool_id", ids)
def test_missing_fields_no_crash(tool_id: str) -> None:
    r = _run(tool_id, _json(MISSING[tool_id]))
    assert isinstance(r, ToolResult)


@pytest.mark.parametrize("body", ["[]", "null", "\"str\"", "42", "{not json", ""])
@pytest.mark.parametrize("tool_id", ids)
def test_malformed_json_fails_cleanly(tool_id: str, body: str) -> None:
    r = _run(tool_id, lambda _r: httpx.Response(200, content=body.encode(),
                                                 headers={"content-type": "application/json"}))
    assert isinstance(r, ToolResult)
    assert r.success is False or r.output.results == []


@pytest.mark.parametrize("status", [403, 429, 500, 503])
@pytest.mark.parametrize("tool_id", ids)
def test_http_errors_fail_cleanly(tool_id: str, status: int) -> None:
    r = _run(tool_id, _json({"error": "x"}, status))
    assert r.success is False
    assert r.error
    assert r.metadata.get("failure_kind")


@pytest.mark.parametrize("exc", [httpx.ReadTimeout, httpx.ConnectError])
@pytest.mark.parametrize("tool_id", ids)
def test_transport_errors_fail_cleanly(tool_id: str, exc: type[httpx.HTTPError]) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise exc("boom", request=request)
    r = _run(tool_id, boom)
    assert r.success is False
    assert r.error


@pytest.mark.parametrize("tool_id", ids)
def test_unicode_query_is_encoded(tool_id: str) -> None:
    seen: list[httpx.Request] = []

    def h(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=EMPTY[tool_id])
    r = _run(tool_id, h, query="పగడపు దిబ్బలు 🌊 café")
    assert isinstance(r, ToolResult)
    assert seen and any("🌊" in v for v in seen[0].url.params.values())


def test_unicode_content_survives_hackernews() -> None:
    body = {"hits": [{"objectID": "1", "title": "Retrieval augmented generation — 日本語 🚀", "url": "https://x.test",
                      "points": 5, "num_comments": 1, "created_at": "2026-01-01T00:00:00Z"}]}
    r = _run(HACKERNEWS_TOOL_ID, _json(body))
    assert r.success
    assert "🚀" in json.dumps(r.output.results, ensure_ascii=False)


# --- routing -----------------------------------------------------------------
ALL = list(SOURCE_ROUTES)


def test_non_programming_query_routes_no_stackexchange() -> None:
    assert "stackexchange_search" not in route_source_tools("marine heatwaves coral bleaching", ALL)


def test_stackexchange_never_crowds_out_earlier_sources() -> None:
    q = "research and community discussion of python bug reports"
    assert route_source_tools(q, ALL) == ["academic_search", "reddit_search"]


def test_java_coffee_not_routed_to_stackexchange() -> None:
    assert "stackexchange_search" not in route_source_tools("java coffee production", ALL)


def test_python_snake_not_routed_to_stackexchange() -> None:
    assert "stackexchange_search" not in route_source_tools("python snake habitat", ALL)


def test_bed_bug_not_routed_to_stackexchange() -> None:
    assert "stackexchange_search" not in route_source_tools("bed bug infestation", ALL)


def test_programming_context_and_hn_phrases_still_route() -> None:
    assert route_source_tools("python asyncio exception when awaiting", ALL) == ["stackexchange_search"]
    assert route_source_tools("bug in my script crashes on startup", ALL)[0] in ("hackernews_search", "stackexchange_search")
    assert "stackexchange_search" in route_source_tools("debugging a python bug", ALL)
    assert "hackernews_search" in route_source_tools("Show HN launches this week", ALL)
    assert "hackernews_search" not in route_source_tools("the hn ratio in chemistry", ALL)
    assert "hackernews_search" not in route_source_tools("john smith", ALL)
