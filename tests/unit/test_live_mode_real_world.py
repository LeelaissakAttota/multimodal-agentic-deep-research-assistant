"""Offline tests: automatic free-model selection/fallback and free search chain."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from deep_research.core.agents.planning_agent import DeterministicPlanningAgent
from deep_research.core.config import Settings
from deep_research.core.free_model_guard import FreeModelGuard, ModelCapability
from deep_research.core.provider_wiring import build_provider_gateway
from deep_research.errors.base import ModelError
from deep_research.models.guarded_gateway import GuardedModelGateway
from deep_research.models.model_gateway import ModelRequest
from deep_research.models.openrouter_catalog import OpenRouterFreeModelCatalog, should_fallback
from deep_research.models.providers.openrouter_gateway import OpenRouterModelGateway
from deep_research.tools.tool import ToolRequest
from deep_research.tools.web_tools import WebSearchTool

MODELS = {
    "data": [
        {"id": "paid/big", "architecture": {"input_modalities": ["text"]}, "context_length": 999999},
        {"id": "small/text:free", "architecture": {"input_modalities": ["text"]}, "context_length": 8_000},
        {"id": "big/text:free", "architecture": {"input_modalities": ["text"]}, "context_length": 200_000},
        {"id": "vis/a:free", "architecture": {"input_modalities": ["text", "image"]}, "context_length": 100_000},
        {"id": "safety/content-safety:free", "architecture": {"input_modalities": ["text", "image"]},
         "context_length": 500_000},
        "garbage",
    ]
}


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class Recorder:
    def __init__(self, outcomes: dict[str, Any]) -> None:
        self.outcomes = outcomes
        self.models_calls = 0
        self.chat_models: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            self.models_calls += 1
            return httpx.Response(200, json=MODELS)
        body = json.loads(request.content)
        model = body["model"]
        self.chat_models.append(model)
        outcome = self.outcomes.get(model, "ok")
        if isinstance(outcome, int):
            msg = "Unauthorized" if outcome in (401, 403) else f"{model} is unavailable for free"
            if outcome == 403:
                msg = f"{model} is only available on agentic harnesses"
            return httpx.Response(outcome, json={"error": {"message": msg}})
        return httpx.Response(200, json={"choices": [{"message": {"content": f"answer from {model}"}}]})


# -- catalog -----------------------------------------------------------------

def test_catalog_filters_free_and_ranks_text_and_vision_separately() -> None:
    rec = Recorder({})
    cat = OpenRouterFreeModelCatalog(api_key=SecretStr("k"), http_client=client(rec))
    text = run(cat.ranked("text"))
    vision = run(cat.ranked("vision"))
    assert "paid/big" not in text and all(m.endswith(":free") for m in text)
    # text-only before multimodal, general before specialty, bigger context first
    assert text[0] == "big/text:free"
    assert text.index("big/text:free") < text.index("small/text:free")
    assert text.index("vis/a:free") < text.index("safety/content-safety:free")
    assert text[-1] == "safety/content-safety:free"
    assert vision == ["vis/a:free", "safety/content-safety:free"]
    assert rec.models_calls == 1  # cached across calls


def test_catalog_pin_goes_first_and_ttl_refresh() -> None:
    rec = Recorder({})
    cat = OpenRouterFreeModelCatalog(api_key=SecretStr("k"), http_client=client(rec), cache_ttl_seconds=0.0)
    assert run(cat.ranked("text", pin="pinned/model:free"))[0] == "pinned/model:free"
    assert run(cat.ranked("text", pin="openai/gpt-4o"))[0] != "openai/gpt-4o"  # non-free pin ignored
    assert rec.models_calls == 2  # ttl 0 -> refetch


def test_catalog_errors_are_model_errors() -> None:
    cat = OpenRouterFreeModelCatalog(api_key=SecretStr("k"), http_client=client(lambda r: httpx.Response(500)))
    with pytest.raises(ModelError):
        run(cat.list_models())
    empty = OpenRouterFreeModelCatalog(
        api_key=SecretStr("k"), http_client=client(lambda r: httpx.Response(200, json={"data": []}))
    )
    with pytest.raises(ModelError) as info:
        run(empty.list_models())
    assert info.value.error_code == "no_free_models"


# -- gateway fallback ----------------------------------------------------------

def test_gateway_falls_back_on_404_and_429() -> None:
    rec = Recorder({"big/text:free": 404, "small/text:free": 429})
    gw = OpenRouterModelGateway(api_key=SecretStr("k"), http_client=client(rec))
    resp = run(gw.generate(ModelRequest(prompt="hi", parameters={})))
    assert resp.model_id == "vis/a:free"
    assert rec.chat_models[:2] == ["big/text:free", "small/text:free"]
    assert rec.models_calls >= 2  # cache refreshed after failure


def test_gateway_pinned_model_then_fallback() -> None:
    rec = Recorder({"meta-llama/llama-3.3-70b-instruct:free": 404})
    gw = OpenRouterModelGateway(api_key=SecretStr("k"), model="meta-llama/llama-3.3-70b-instruct:free",
                                http_client=client(rec))
    resp = run(gw.generate(ModelRequest(prompt="hi", parameters={})))
    assert rec.chat_models[0] == "meta-llama/llama-3.3-70b-instruct:free"
    assert resp.model_id.startswith(("vis/", "big/", "small/"))


def test_gateway_vision_requests_use_only_vision_models() -> None:
    rec = Recorder({"vis/a:free": 429})
    gw = OpenRouterModelGateway(api_key=SecretStr("k"), http_client=client(rec), kind="vision")
    resp = run(gw.generate(ModelRequest(prompt="describe", image_data="data:image/png;base64,AAAA", parameters={})))
    assert rec.chat_models == ["vis/a:free", "safety/content-safety:free"]
    assert resp.model_id == "safety/content-safety:free"


def test_gateway_auth_error_does_not_fallback() -> None:
    rec = Recorder({"big/text:free": 401})
    gw = OpenRouterModelGateway(api_key=SecretStr("k"), http_client=client(rec))
    with pytest.raises(ModelError) as info:
        run(gw.generate(ModelRequest(prompt="hi", parameters={})))
    assert info.value.error_code == "provider_auth_error"
    assert rec.chat_models == ["big/text:free"]


def test_gateway_gives_up_after_max_attempts() -> None:
    rec = Recorder({m["id"]: 429 for m in MODELS["data"] if isinstance(m, dict)})
    gw = OpenRouterModelGateway(api_key=SecretStr("k"), http_client=client(rec), max_attempts=2)
    with pytest.raises(ModelError):
        run(gw.generate(ModelRequest(prompt="hi", parameters={})))
    assert len(rec.chat_models) == 2


def test_should_fallback_classification() -> None:
    assert should_fallback(ModelError("x", error_code="provider_rate_limit"))
    assert should_fallback(ModelError("x", error_code="provider_client_error", details={"status_code": 404}))
    assert should_fallback(ModelError("model is unavailable for free", error_code="other"))
    assert not should_fallback(ModelError("bad key", error_code="provider_auth_error", details={"status_code": 401}))


def test_wiring_defaults_to_auto_and_guard_still_blocks_paid() -> None:
    s = Settings(_env_file=None, openrouter_api_key=SecretStr("k"))
    assert s.openrouter_model == "auto" and s.search_provider == "auto"
    g = build_provider_gateway(s, capability=ModelCapability.TEXT_GENERATION)
    assert isinstance(g, GuardedModelGateway) and g.model_id == "auto:free"
    paid = build_provider_gateway(Settings(_env_file=None, openrouter_api_key=SecretStr("k"),
                                           openrouter_model="openai/gpt-4o"))
    assert paid is not None
    with pytest.raises(ModelError) as info:
        run(paid.generate(ModelRequest(prompt="x", parameters={})))
    assert info.value.error_code == "paid_model_rejected"
    assert FreeModelGuard().is_free("openrouter", "auto:free")


# -- search chain --------------------------------------------------------------

DDG_LITE = """<html><body><table>
<tr><td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.iaea.org%2Fsmr&amp;rut=1"
 class='result-link'>What are SMRs? | IAEA</a></td></tr>
<tr><td><a rel="nofollow" href="https://en.wikipedia.org/wiki/Small_modular_reactor">Small modular reactor</a></td></tr>
</table></body></html>"""
CHALLENGE = "<html><body><div class='anomaly-modal'>Unfortunately, bots use DuckDuckGo too.</div></body></html>"
WIKI = {"query": {"search": [
    {"title": "Small modular reactor", "snippet": "A class of <span class=\"searchmatch\">fission</span> reactor"},
    {"title": "", "snippet": "skip"},
]}}


def search(handler: Any, **kwargs: Any) -> Any:
    tool = WebSearchTool(http_client=client(handler), **kwargs)
    return run(tool.execute(ToolRequest(tool_name="s", parameters={"query": "small modular reactors", "limit": 5})))


def test_ddg_202_challenge_falls_back_to_lite() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.host == "html.duckduckgo.com":
            return httpx.Response(202, text=CHALLENGE)
        if r.url.host == "lite.duckduckgo.com":
            assert r.url.params["q"] == "small modular reactors"
            return httpx.Response(200, text=DDG_LITE)
        raise AssertionError(str(r.url))

    res = search(handler)
    assert res.success and res.metadata["provider"] == "duckduckgo_lite"
    urls = [h["url"] for h in res.output.results]
    assert urls == ["https://www.iaea.org/smr", "https://en.wikipedia.org/wiki/Small_modular_reactor"]


def test_challenge_200_page_also_skipped_then_wikipedia() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        if "duckduckgo" in str(r.url.host) and r.url.host != "api.duckduckgo.com":
            return httpx.Response(200, text=CHALLENGE)
        if r.url.host == "en.wikipedia.org":
            return httpx.Response(200, json=WIKI)
        raise AssertionError(str(r.url))

    res = search(handler)
    assert res.success and res.metadata["provider"] == "wikipedia"
    assert res.output.results[0]["snippet"] == "A class of fission reactor"
    assert res.output.results[0]["url"] == "https://en.wikipedia.org/wiki/Small_modular_reactor"


def test_instant_answer_last_resort_and_tavily_first_when_keyed() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.host == "api.duckduckgo.com":
            return httpx.Response(200, json={"Heading": "SMR", "AbstractURL": "https://en.wikipedia.org/wiki/SMR",
                                             "Abstract": "Abstract text", "RelatedTopics": [
                                                 {"Topics": [{"FirstURL": "https://duckduckgo.com/c/x", "Text": "X"}]}
                                             ]})
        if r.url.host == "en.wikipedia.org":
            return httpx.Response(200, json={"query": {"search": []}})
        if r.url.host == "api.tavily.com":
            return httpx.Response(200, json={"results": [{"title": "T", "url": "https://t.example/x", "content": "c"}]})
        return httpx.Response(202, text="")

    res = search(handler)
    assert res.metadata["provider"] == "duckduckgo_instant"
    assert [h["url"] for h in res.output.results][:1] == ["https://en.wikipedia.org/wiki/SMR"]
    keyed = search(handler, tavily_api_key=SecretStr("tk"))
    assert keyed.metadata["provider"] == "tavily"


def test_all_sources_down_is_failure() -> None:
    res = search(lambda r: httpx.Response(503))
    assert not res.success and res.metadata["failure_kind"] == "http_error"


def test_all_sources_blocked_is_empty_success() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.host in ("en.wikipedia.org", "api.duckduckgo.com"):
            return httpx.Response(200, json={} if r.url.host == "api.duckduckgo.com" else {"query": {"search": []}})
        return httpx.Response(202, text=CHALLENGE)

    res = search(handler)
    assert res.success and res.output.results == []


def test_planner_does_not_prefix_latest() -> None:
    from deep_research.domain.research.research_state import ResearchState
    from deep_research.domain.research_request import ResearchRequest

    request = ResearchRequest(objective="benefits and risks of small modular reactors")
    state = ResearchState(request_id=request.id)
    plan = run(DeterministicPlanningAgent().create_plan(request, state))
    queries = [str(t.tool_input["query"]) for t in plan.tasks if t.tool_input and "query" in t.tool_input]
    assert queries and not any(q.lower().startswith("latest") for q in queries)
    assert queries[0] == "benefits and risks of small modular reactors"


def test_report_renders_live_findings_with_global_citations() -> None:
    from deep_research.core.agents.report_agent import DeterministicReportAgent
    from deep_research.domain.research.research_state import ResearchState

    state = ResearchState(request_id=__import__("uuid").uuid4())
    state.metadata["live_findings"] = [
        {"objective": "Benefits", "summary": "Cheap [1] and safe [2].", "model_id": "m/a:free",
         "sources": [{"source_id": "s1", "url": "https://a.example/1", "title": "A"},
                     {"source_id": "s2", "url": "https://b.example/2", "title": "B"}]},
        {"objective": "Risks", "summary": "Waste [1], cost [2, 9].", "model_id": "m/a:free",
         "sources": [{"source_id": "s3", "url": "https://c.example/3", "title": "C"},
                     {"source_id": "s1", "url": "https://a.example/1", "title": "A"}]},
    ]
    report = run(DeterministicReportAgent().generate_report(state, {"claims": [], "summary": "x"}))
    assert [c["url"] for c in report["citations"]] == ["https://a.example/1", "https://b.example/2", "https://c.example/3"]
    assert "Cheap [1] and safe [2]." in report["content"]
    assert "Waste [3], cost [1]." in report["content"]
    assert "## Sources" in report["content"] and report["models_used"] == ["m/a:free"]


def test_report_offline_unchanged_without_live_findings() -> None:
    from deep_research.core.agents.report_agent import DeterministicReportAgent
    from deep_research.domain.research.research_state import ResearchState

    state = ResearchState(request_id=__import__("uuid").uuid4())
    report = run(DeterministicReportAgent().generate_report(state, {"claims": [], "summary": "plain"}))
    assert report["content"] == "plain" and report["citations"] == [] and "models_used" not in report
