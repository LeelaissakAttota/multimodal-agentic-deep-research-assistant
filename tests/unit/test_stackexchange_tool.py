"""Stack Exchange search tool, all network mocked."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

import httpx
import pytest

from deep_research.domain.modality import Modality
from deep_research.tools.stackexchange_tool import (
    STACKEXCHANGE_TOOL_ID,
    StackExchangeSearchTool,
    pick_sites,
    safe_se_link,
)
from deep_research.tools.tool import ToolRequest


def _items(site: str) -> dict[str, Any]:
    host = "stackoverflow.com" if site == "stackoverflow" else f"{site}.stackexchange.com"
    return {"items": [
        {"title": "How to interpret a p-value &amp; confidence interval?", "link": f"https://{host}/questions/1/x",
         "score": 42, "answer_count": 3, "is_answered": True, "creation_date": 1700000000, "tags": ["p-value"],
         "body": "<p>I ran a <b>regression</b>...</p>"},
        {"title": "p-value confidence interval on localhost", "link": "http://127.0.0.1/questions/2", "score": 1},
        {"title": "p-value confidence interval elsewhere", "link": "https://evil.example.com/questions/3"},
        {"title": "Unrelated gardening question", "link": f"https://{host}/questions/4/y", "score": 99},
        "junk",
    ], "has_more": False, "quota_max": 300, "quota_remaining": 250}


def _client(handler: Callable[[httpx.Request], httpx.Response], calls: list[httpx.Request]) -> httpx.AsyncClient:
    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)
    return httpx.AsyncClient(transport=httpx.MockTransport(wrapped))


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=_items(request.url.params["site"]))


class _Sleeper:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def _run(tool: StackExchangeSearchTool, **params: Any) -> Any:
    params.setdefault("query", "p-value confidence interval")
    return asyncio.run(tool.execute(ToolRequest(tool_name=STACKEXCHANGE_TOOL_ID, parameters=params)))


def test_definition_is_free_and_keyless() -> None:
    d = StackExchangeSearchTool().get_definition()
    assert d.modality == Modality.SOCIAL and d.cost_class == "FREE" and not d.auth_required


def test_default_site_is_stackoverflow_and_fields_are_returned() -> None:
    calls: list[httpx.Request] = []
    result = _run(StackExchangeSearchTool(http_client=_client(_ok, calls)), query="asyncio gather confidence interval p-value")
    assert result.success
    q = calls[0].url.params
    assert calls[0].url.host == "api.stackexchange.com" and calls[0].url.path == "/2.3/search/advanced"
    assert q["site"] == "stats" and q["sort"] == "relevance" and "filter" not in q
    row = result.output.results[0]
    assert row["title"] == "How to interpret a p-value & confidence interval?"
    assert row["url"] == "https://stats.stackexchange.com/questions/1/x"
    assert (row["score"], row["answer_count"], row["is_answered"], row["site"]) == ("42", "3", "true", "stats")
    assert row["creation_date"] == "2023-11-14T22:13:20Z" and row["source_type"] == "social"
    assert [r["url"] for r in result.output.results] == [row["url"]]  # SSRF / foreign host / off-topic dropped
    assert result.metadata["quota_remaining"] == 250
    plain = _run(StackExchangeSearchTool(http_client=_client(_ok, [])), query="python asyncio gather")
    assert plain.metadata["provider"] == "stackoverflow"


def test_topic_sites_are_picked_and_explicit_sites_validated() -> None:
    assert pick_sites("python list comprehension") == ["stackoverflow"]
    assert pick_sites("regression p-value for a physics experiment") == ["stats", "physics"]
    assert pick_sites("black-scholes option pricing and tax on investing") == ["quant", "money"]
    assert pick_sites("statistics math physics trading money") == ["stats", "math", "physics"]
    calls: list[httpx.Request] = []
    result = _run(StackExchangeSearchTool(http_client=_client(_ok, calls)), sites=["Math", "stats"], with_body=True)
    assert [c.url.params["site"] for c in calls] == ["math", "stats"] and calls[0].url.params["filter"] == "withbody"
    assert result.metadata["provider"] == "math,stats"
    bad = _run(StackExchangeSearchTool(http_client=_client(_ok, [])), site="../etc")
    assert bad.metadata["failure_kind"] == "invalid_input"


def test_short_backoff_is_honoured_before_next_site(caplog: Any) -> None:
    sleeper = _Sleeper()

    def handler(request: httpx.Request) -> httpx.Response:
        body = _items(request.url.params["site"])
        body["backoff"] = 2
        return httpx.Response(200, json=body)
    with caplog.at_level(logging.WARNING, logger="deep_research.tools.stackexchange_tool"):
        result = _run(StackExchangeSearchTool(http_client=_client(handler, []), sleep=sleeper), sites=["stats", "math"])
    assert result.success and len(sleeper.waits) == 1 and 0 < sleeper.waits[0] <= 2
    assert "asked to back off 2s" in caplog.text


def test_long_backoff_stops_remaining_sites(caplog: Any) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = _items(request.url.params["site"])
        body["backoff"] = 60
        return httpx.Response(200, json=body)
    with caplog.at_level(logging.WARNING, logger="deep_research.tools.stackexchange_tool"):
        result = _run(StackExchangeSearchTool(http_client=_client(handler, calls), sleep=_Sleeper()),
                      sites=["stats", "math", "physics"])
    assert len(calls) == 1 and result.success and result.metadata["provider_errors"] == {"math": "backoff"}
    assert "too long; stopping" in caplog.text


def test_low_quota_stops_querying(caplog: Any) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = _items(request.url.params["site"])
        body["quota_remaining"] = 3
        return httpx.Response(200, json=body)
    with caplog.at_level(logging.WARNING, logger="deep_research.tools.stackexchange_tool"):
        result = _run(StackExchangeSearchTool(http_client=_client(handler, calls)), sites=["stats", "math"])
    assert len(calls) == 1 and result.metadata["quota_remaining"] == 3
    assert result.metadata["provider_errors"] == {"math": "quota_low"}
    assert "quota nearly exhausted" in caplog.text and "quota low (3 left); skipping math" in caplog.text


def test_throttle_violation_and_http_errors_are_typed() -> None:
    def throttled(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error_id": 502, "error_name": "throttle_violation"})
    res = _run(StackExchangeSearchTool(http_client=_client(throttled, [])))
    assert not res.success and res.metadata["failure_kind"] == "rate_limited"
    assert "falling back to web search" in (res.error or "")
    res = _run(StackExchangeSearchTool(http_client=_client(lambda r: httpx.Response(500), [])))
    assert res.metadata["failure_kind"] == "http_500"


def test_retry_on_429_then_partial_site_failure() -> None:
    seen: list[httpx.Request] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429) if len(seen) == 1 else _ok(request)
    assert _run(StackExchangeSearchTool(http_client=_client(flaky, seen))).success and len(seen) == 2

    def math_down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503) if request.url.params["site"] == "math" else _ok(request)
    res = _run(StackExchangeSearchTool(http_client=_client(math_down, [])), sites=["math", "stats"])
    assert res.success and res.metadata["provider_errors"] == {"math": "http_503"} and res.metadata["provider"] == "stats"


def test_malformed_and_network_errors_do_not_crash() -> None:
    res = _run(StackExchangeSearchTool(http_client=_client(lambda r: httpx.Response(200, json=["x"]), [])))
    assert res.metadata["failure_kind"] == "malformed_response"

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)
    assert _run(StackExchangeSearchTool(http_client=_client(boom, []))).metadata["failure_kind"] == "timeout"
    assert _run(StackExchangeSearchTool(http_client=_client(boom, [])), query="").metadata["failure_kind"] == "invalid_input"


@pytest.mark.parametrize(("url", "kept"), [
    ("https://stackoverflow.com/questions/1", True), ("https://stats.stackexchange.com/q/1", True),
    ("https://mathoverflow.net/questions/1", True), ("http://stackoverflow.com/questions/1", False),
    ("https://stackoverflow.com.evil.example/q", False), ("https://10.0.0.1/q", False), (None, False),
])
def test_safe_se_link(url: Any, kept: bool) -> None:
    assert bool(safe_se_link(url)) is kept
