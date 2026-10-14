"""Offline tests for live web tools, LiveResearchAgent and mode selection."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from deep_research.api.research_service import ResearchApplication, ResearchSubmission
from deep_research.core.agents.deterministic_research_agent import DeterministicResearchAgent
from deep_research.core.agents.live_research_agent import LiveResearchAgent
from deep_research.core.config import Settings
from deep_research.domain.research.research_task import ResearchTask
from deep_research.errors.base import ModelError
from deep_research.models.model_gateway import ModelGateway, ModelRequest, ModelResponse
from deep_research.tools.tool import ToolRequest
from deep_research.tools.web_tools import (
    WebFetchTool,
    WebSearchTool,
    html_to_text,
    is_public_http_url,
)

DDG_HTML = """
<html><body>
<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fa&rut=x">Alpha
 page</a><a class="result__snippet" href="#">Alpha snippet text</a></div>
<div class="result"><a class="result__a" href="https://example.net/b">Beta</a>
<a class="result__snippet">Beta snippet</a></div>
<div class="result"><a class="result__a" href="/y.js?ad=1">Ad</a></div>
</body></html>
"""
PAGE = "<html><head><title>Alpha Title</title><script>evil()</script></head><body><p>Alpha body facts.</p></body></html>"


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def mock_client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def always_public(url: str) -> bool:
    return url.startswith(("http://", "https://"))


def web_handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "html.duckduckgo.com":
        return httpx.Response(200, text=DDG_HTML)
    if request.url.host == "example.org":
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})
    return httpx.Response(404)


class FakeLLM(ModelGateway):
    def __init__(self, result: Any = "Alpha is a fact [1].") -> None:
        self.result = result
        self.prompts: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.prompts.append(request)
        if isinstance(self.result, BaseException):
            raise self.result
        return ModelResponse(text=self.result, model_id="fake/model:free")

    async def health_check(self) -> bool:
        return True


def agent(gateway: ModelGateway | None = None, handler: Any = web_handler) -> LiveResearchAgent:
    client = mock_client(handler)
    return LiveResearchAgent(
        search_tool=WebSearchTool(http_client=client),
        fetch_tool=WebFetchTool(http_client=client, url_validator=always_public),
        gateway=gateway,
        max_fetch=2,
    )


def task(objective: str = "What is alpha?") -> ResearchTask:
    return ResearchTask(plan_id=uuid4(), description=objective, objective=objective)


# -- search ------------------------------------------------------------------

def test_ddg_search_parses_and_unwraps() -> None:
    tool = WebSearchTool(http_client=mock_client(web_handler))
    res = run(tool.execute(ToolRequest(tool_name="s", parameters={"query": "alpha"})))
    assert res.success
    results = res.output.results
    assert [r["url"] for r in results] == ["https://example.org/a", "https://example.net/b"]
    assert results[0]["title"] == "Alpha page" and results[0]["snippet"] == "Alpha snippet text"


def test_search_invalid_query_and_http_error() -> None:
    tool = WebSearchTool(http_client=mock_client(lambda r: httpx.Response(503)))
    assert run(tool.execute(ToolRequest(tool_name="s", parameters={}))).metadata["failure_kind"] == "invalid_input"
    assert run(tool.execute(ToolRequest(tool_name="s", parameters={"query": "x"}))).metadata["failure_kind"] == "http_error"


def test_tavily_backend() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        assert r.headers["authorization"] == "Bearer tk"
        return httpx.Response(200, json={"results": [{"title": "T", "url": "https://t.io", "content": "c"}]})

    tool = WebSearchTool(provider="tavily", tavily_api_key=SecretStr("tk"), http_client=mock_client(handler))
    res = run(tool.execute(ToolRequest(tool_name="s", parameters={"query": "x"})))
    assert res.output.results == [{"title": "T", "url": "https://t.io", "snippet": "c"}]
    with pytest.raises(ValueError):
        WebSearchTool(provider="tavily")


# -- fetch -------------------------------------------------------------------

def test_fetch_extracts_text_and_skips_scripts() -> None:
    tool = WebFetchTool(http_client=mock_client(web_handler), url_validator=always_public)
    res = run(tool.execute(ToolRequest(tool_name="f", parameters={"url": "https://example.org/a"})))
    assert res.success and res.output.title == "Alpha Title"
    assert "Alpha body facts." in res.output.text and "evil" not in res.output.text


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://10.0.0.1/x", "http://[::1]/", "file:///etc/passwd",
                                 "ftp://example.org", "http://user:pw@8.8.8.8/", "http://169.254.169.254/latest"])
def test_ssrf_guard_blocks(url: str) -> None:
    assert not is_public_http_url(url)
    res = run(WebFetchTool(http_client=mock_client(web_handler)).execute(ToolRequest(tool_name="f", parameters={"url": url})))
    assert res.metadata["failure_kind"] == "blocked_url"


def test_ssrf_guard_resolver() -> None:
    assert is_public_http_url("https://example.org/", resolver=lambda h: ["93.184.216.34"])
    assert not is_public_http_url("https://evil.test/", resolver=lambda h: ["192.168.1.5"])


def test_fetch_blocks_private_redirect() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

    validator = lambda u: "127.0.0.1" not in u  # noqa: E731
    res = run(WebFetchTool(http_client=mock_client(handler), url_validator=validator).execute(
        ToolRequest(tool_name="f", parameters={"url": "https://example.org/"})))
    assert res.metadata["failure_kind"] == "blocked_url"


def test_fetch_size_type_and_redirect_limits() -> None:
    big = mock_client(lambda r: httpx.Response(200, text="x" * 500, headers={"content-type": "text/plain"}))
    res = run(WebFetchTool(http_client=big, max_bytes=100, url_validator=always_public).execute(
        ToolRequest(tool_name="f", parameters={"url": "https://a.io"})))
    assert res.metadata["failure_kind"] == "too_large"
    pdf = mock_client(lambda r: httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"}))
    res = run(WebFetchTool(http_client=pdf, url_validator=always_public).execute(
        ToolRequest(tool_name="f", parameters={"url": "https://a.io"})))
    assert res.metadata["failure_kind"] == "unsupported_type"
    loop = mock_client(lambda r: httpx.Response(302, headers={"location": "https://a.io/again"}))
    res = run(WebFetchTool(http_client=loop, max_redirects=2, url_validator=always_public).execute(
        ToolRequest(tool_name="f", parameters={"url": "https://a.io"})))
    assert res.metadata["failure_kind"] == "too_many_redirects"


def test_html_to_text() -> None:
    assert html_to_text(PAGE) == ("Alpha Title", "Alpha body facts.")


# -- agent -------------------------------------------------------------------

def test_agent_llm_synthesis_with_citations() -> None:
    llm = FakeLLM()
    res = run(agent(llm).execute_task(task()))
    assert res.success and res.output["summary"] == "Alpha is a fact [1]."
    assert len(res.output["evidence_ids"]) == 2 == len(res.output["source_ids"])
    ev = res.output["evidence_objects"]
    assert ev[0].content.startswith("Alpha body facts") and ev[0].metadata["fetched"] is True
    assert ev[1].metadata["fetched"] is False  # 404 fetch -> snippet kept
    prompt = llm.prompts[0]
    assert "[1]" in prompt.prompt and "What is alpha?" in prompt.prompt
    assert prompt.parameters["required_capability"] == "text_generation"
    assert res.metadata["synthesis"] == "llm"


def test_agent_without_llm_uses_snippets() -> None:
    res = run(agent(None).execute_task(task()))
    assert res.success and res.metadata["synthesis"] == "snippets"
    assert "[1]" in res.output["summary"]


@pytest.mark.parametrize("result", [ModelError("x", error_code="paid_model_rejected"), RuntimeError("x"), "  "])
def test_agent_llm_failure_falls_back(result: Any) -> None:
    res = run(agent(FakeLLM(result)).execute_task(task()))
    assert res.success and res.metadata["synthesis"] == "snippets" and res.metadata["synthesis_error"]


def test_agent_search_failure_and_no_results() -> None:
    res = run(agent(handler=lambda r: httpx.Response(500)).execute_task(task()))
    assert not res.success and res.metadata["stage"] == "search"
    res = run(agent(handler=lambda r: httpx.Response(200, text="<html></html>")).execute_task(task()))
    assert not res.success and res.metadata["failure_kind"] == "no_results"


def test_agent_uses_tool_input_query() -> None:
    seen: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.host == "html.duckduckgo.com":
            seen.append(r.content.decode())
        return web_handler(r)

    t = task()
    t.tool_input = {"query": "custom q"}
    run(agent(handler=handler).execute_task(t))
    assert "custom+q" in seen[0]


# -- mode selection ----------------------------------------------------------

def test_default_mode_is_offline_deterministic() -> None:
    s = Settings(_env_file=None)
    assert s.research_mode == "offline"
    assert isinstance(ResearchApplication(s)._build_research_agent(), DeterministicResearchAgent)


def test_live_mode_builds_live_agent() -> None:
    s = Settings(_env_file=None, research_mode="live")
    a = ResearchApplication(s)._build_research_agent()
    assert isinstance(a, LiveResearchAgent) and a.gateway is None
    s2 = Settings(_env_file=None, research_mode="live", openrouter_api_key=SecretStr("k"))
    assert ResearchApplication(s2)._build_research_agent().gateway is not None  # type: ignore[attr-defined]


def test_offline_end_to_end_still_works() -> None:
    app = ResearchApplication(Settings(_env_file=None))
    resp = run(app.submit(ResearchSubmission(objective="Explain photosynthesis")))
    assert resp.status in {"completed", "COMPLETED"} or resp.report is not None


def test_live_end_to_end_with_mocked_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    app = ResearchApplication(Settings(_env_file=None, research_mode="live"))
    monkeypatch.setattr(app, "_build_research_agent", lambda: agent(FakeLLM()))
    resp = run(app.submit(ResearchSubmission(objective="What is alpha?")))
    assert resp.error is None, resp.error
