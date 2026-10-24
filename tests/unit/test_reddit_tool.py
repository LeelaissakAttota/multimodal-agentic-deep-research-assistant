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
    assert not blocked.success and blocked.metadata["failure_kind"] == "http_429"
