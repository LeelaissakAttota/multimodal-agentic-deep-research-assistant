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
    assert "Reddit blocked anonymous request (HTTP 403)" in caplog.text
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


def _oauth_client(calls: list[httpx.Request], token_status: int = 200) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/api/v1/access_token":
            if token_status != 200:
                return httpx.Response(token_status)
            return httpx.Response(200, json={"access_token": "tok123", "expires_in": 3600})
        if request.url.host == "oauth.reddit.com" and request.url.path == "/search":
            return httpx.Response(200, json=SEARCH)
        if request.url.host == "oauth.reddit.com":
            return httpx.Response(200, json=COMMENTS)
        if request.url.path.endswith("search.json"):
            return httpx.Response(200, json=SEARCH)
        return httpx.Response(200, json=COMMENTS)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_oauth_app_only_flow_uses_bearer_and_oauth_host() -> None:
    calls: list[httpx.Request] = []
    tool = RedditSearchTool(http_client=_oauth_client(calls), client_id="cid", client_secret="sec", user_agent="ua/1")
    result = _run(tool, query="rag", comments=1)
    assert result.success and result.output.provider == "reddit_oauth"
    token_req = calls[0]
    assert token_req.method == "POST" and token_req.headers["authorization"].startswith("Basic ")
    assert b"grant_type=client_credentials" in token_req.content
    search = calls[1]
    assert search.url.host == "oauth.reddit.com" and search.headers["authorization"] == "bearer tok123"
    assert search.headers["user-agent"] == "ua/1"
    assert result.output.results[0]["url"].startswith("https://www.reddit.com/r/")
    assert all(c.url.host == "oauth.reddit.com" for c in calls[1:])


def test_oauth_token_failure_falls_back_to_public_json(caplog) -> None:
    import logging

    calls: list[httpx.Request] = []
    tool = RedditSearchTool(http_client=_oauth_client(calls, token_status=401), client_id="cid", client_secret="bad")
    with caplog.at_level(logging.WARNING, logger="deep_research.tools.reddit_tool"):
        result = _run(tool, query="rag")
    assert result.success and result.output.provider == "reddit_json"
    assert "OAuth token request failed (401)" in caplog.text and "bad" not in caplog.text


def test_env_credentials_ignored_with_injected_client(monkeypatch) -> None:
    monkeypatch.setenv("REDDIT_CLIENT_ID", "x")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "y")
    assert not RedditSearchTool(http_client=_client([])).oauth_configured
    assert RedditSearchTool().oauth_configured
    monkeypatch.delenv("REDDIT_CLIENT_ID")
    assert not RedditSearchTool().oauth_configured


def test_credentials_read_from_dotenv_file(monkeypatch, tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text("REDDIT_CLIENT_ID=abc\nREDDIT_CLIENT_SECRET=def\n", encoding="utf-8")
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("DEEP_RESEARCH_ENV_FILE", str(env))
    assert RedditSearchTool().oauth_configured
