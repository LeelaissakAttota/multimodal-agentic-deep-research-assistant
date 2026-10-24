"""Reddit public-JSON tool, all network mocked."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from deep_research.domain.modality import Modality
from deep_research.tools.reddit_tool import REDDIT_TOOL_ID, RedditSearchTool
from deep_research.tools.tool import ToolRequest

SEARCH = {"data": {"children": [
    {"kind": "t3", "data": {"title": "Is RAG dead?", "permalink": "/r/MachineLearning/comments/a1/is_rag_dead/",
                            "selftext": "Long context models &amp; retrieval", "subreddit": "MachineLearning",
                            "score": 120, "num_comments": 30, "author": "u1", "url": "https://www.reddit.com/r/x"}},
    {"kind": "t3", "data": {"title": "NSFW thing", "permalink": "/r/x/comments/a2/", "over_18": True}},
    {"kind": "t3", "data": {"title": "Link post", "permalink": "/r/LocalLLaMA/comments/a3/link/", "selftext": "",
                            "url": "https://arxiv.org/abs/1", "subreddit": "LocalLLaMA"}},
]}}
COMMENTS = [{"data": {}}, {"data": {"children": [
    {"kind": "t1", "data": {"body": "No, it is not."}},
    {"kind": "t1", "data": {"body": "[deleted]"}},
    {"kind": "more", "data": {}},
]}}]


def _client(calls: list[httpx.Request], status: int = 200) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if status != 200:
            return httpx.Response(status)
        if request.url.path.endswith("search.json"):
            return httpx.Response(200, json=SEARCH)
        return httpx.Response(200, json=COMMENTS)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _run(tool: RedditSearchTool, **params: Any) -> Any:
    return asyncio.run(tool.execute(ToolRequest(tool_name=REDDIT_TOOL_ID, parameters=params)))


def test_definition() -> None:
    d = RedditSearchTool().get_definition()
    assert d.modality == Modality.SOCIAL and d.cost_class == "FREE" and not d.auth_required


def test_search_parses_posts_and_skips_nsfw() -> None:
    calls: list[httpx.Request] = []
    result = _run(RedditSearchTool(http_client=_client(calls)), query="rag long context")
    assert result.success
    rows = result.output.results
    assert [r["title"] for r in rows] == ["Is RAG dead?", "Link post"]
    assert rows[0]["url"] == "https://www.reddit.com/r/MachineLearning/comments/a1/is_rag_dead/"
    assert rows[0]["content"] == "Long context models & retrieval" and rows[0]["source_type"] == "social"
    assert "links to https://arxiv.org/abs/1" in rows[1]["content"]
    assert calls[0].headers["User-Agent"].startswith("deep-research")
    assert calls[0].url.path == "/search.json"


def test_subreddit_and_comments() -> None:
    calls: list[httpx.Request] = []
    result = _run(RedditSearchTool(http_client=_client(calls)), query="rag", subreddit="r/MachineLearning",
                  comments=2, limit=1)
    assert calls[0].url.path == "/r/MachineLearning/search.json"
    assert calls[0].url.params["restrict_sr"] == "on"
    assert "Top comments: No, it is not." in result.output.results[0]["content"]
    assert "[deleted]" not in result.output.results[0]["content"]


def test_invalid_inputs_and_http_errors() -> None:
    tool = RedditSearchTool(http_client=_client([]))
    assert _run(tool, query="").metadata["failure_kind"] == "invalid_input"
    assert _run(tool, query="x", subreddit="bad name!").metadata["failure_kind"] == "invalid_input"
    blocked = _run(RedditSearchTool(http_client=_client([], status=429)), query="x")
    assert not blocked.success and blocked.metadata["failure_kind"] == "rate_limited"


def test_blocked_requests_log_clear_warning_and_return_typed_error(caplog: Any) -> None:
    import logging

    with caplog.at_level(logging.WARNING, logger="deep_research.tools.reddit_tool"):
        result = _run(RedditSearchTool(http_client=_client([], status=403)), query="x")
    assert not result.success and result.metadata["failure_kind"] == "blocked"
    assert result.metadata["http_status"] == 403
    assert "Reddit blocked request (HTTP 403), likely datacenter IP; falling back to web search" in caplog.text
    assert result.error and "datacenter IP" in result.error
    other = _run(RedditSearchTool(http_client=_client([], status=500)), query="x")
    assert other.metadata["failure_kind"] == "http_500"


def test_live_agent_logs_and_falls_back_when_reddit_blocked(caplog: Any) -> None:
    import logging

    from deep_research.core.agents.live_research_agent import LiveResearchAgent
    from deep_research.domain.research.research_task import ResearchTask
    from deep_research.tools.web_tools import WebFetchTool, WebSearchTool
    from uuid import uuid4

    ddg = ('<div class="result"><a class="result__a" href="https://example.org/a">A</a>'
           '<a class="result__snippet">Alpha snippet</a></div>')

    def web(request: httpx.Request) -> httpx.Response:
        if request.url.host == "html.duckduckgo.com":
            return httpx.Response(200, text=ddg)
        if request.url.host == "www.reddit.com":
            return httpx.Response(403)
        return httpx.Response(404)

    c = httpx.AsyncClient(transport=httpx.MockTransport(web))
    agent = LiveResearchAgent(search_tool=WebSearchTool(http_client=c), media_tools={},
                              fetch_tool=WebFetchTool(http_client=c, url_validator=lambda u: True),
                              source_tools={"reddit_search": RedditSearchTool(http_client=c)})
    task = ResearchTask(plan_id=uuid4(), description="d", objective="o", assigned_tool="reddit_search",
                        tool_input={"query": "alpha"})
    with caplog.at_level(logging.WARNING):
        result = asyncio.run(agent.execute_task(task))
    assert result.success and result.metadata["source_tool_fallback"] == "blocked"
    assert "falling back to web search for this task" in caplog.text
