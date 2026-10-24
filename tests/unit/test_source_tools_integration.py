"""Phase 2 wiring: registry, planner routing, live agent source tasks, citations (all offline)."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

import httpx
import pytest

from deep_research.api.research_service import ResearchApplication, ResearchSubmission
from deep_research.core.agents.live_research_agent import LiveResearchAgent, source_hits_to_evidence
from deep_research.core.agents.llm_agents import LlmPlanningAgent
from deep_research.core.agents.planning_agent import DeterministicPlanningAgent, route_source_tools
from deep_research.core.agents.report_agent import _citation_line
from deep_research.core.config import Settings
from deep_research.core.orchestration.master_research_orchestrator import _source_row
from deep_research.domain.research.research_state import ResearchState
from deep_research.domain.research.research_task import ResearchTask
from deep_research.domain.research_request import ResearchRequest
from deep_research.models.model_gateway import ModelGateway, ModelRequest, ModelResponse
from deep_research.tools.academic_tool import AcademicSearchTool
from deep_research.tools.registry import SOURCE_TOOL_IDS, build_source_tools, default_registry, parse_source_tools
from deep_research.tools.selector import ToolSelector
from deep_research.tools.tool import Tool, ToolRequest, ToolResult
from deep_research.tools.web_tools import WebFetchTool, WebSearchTool

DDG = ('<div class="result"><a class="result__a" href="https://example.org/a">Alpha page</a>'
       '<a class="result__snippet">Alpha snippet</a></div>')
PAGE = "<html><head><title>Alpha</title></head><body><p>Alpha body facts.</p></body></html>"
S2 = {"data": [{"title": "Fasting trial", "url": "https://www.semanticscholar.org/paper/f1",
                "abstract": "A randomized trial of intermittent fasting.", "year": 2022,
                "authors": [{"name": "A. Author"}, {"name": "B. Author"}], "venue": "NEJM", "externalIds": {}}]}


def handler(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    if host == "html.duckduckgo.com":
        return httpx.Response(200, text=DDG)
    if host == "example.org":
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})
    if host == "api.semanticscholar.org":
        return httpx.Response(200, json=S2)
    if host in ("export.arxiv.org", "api.openalex.org"):
        return httpx.Response(503)
    return httpx.Response(404)


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def live_agent(source_tools: dict[str, Tool]) -> LiveResearchAgent:
    c = client()
    return LiveResearchAgent(search_tool=WebSearchTool(http_client=c),
                             fetch_tool=WebFetchTool(http_client=c, url_validator=lambda u: True),
                             media_tools={}, source_tools=source_tools)


class FailingTool(Tool):
    @property
    def name(self) -> str:
        return "reddit_search"

    def get_definition(self) -> Any:  # pragma: no cover - not used
        raise NotImplementedError

    async def health_check(self) -> bool:
        return True

    async def execute(self, request: ToolRequest) -> ToolResult:
        return ToolResult(success=False, error="blocked", tool_name="reddit_search",
                          metadata={"failure_kind": "http_429"})


class PlanLLM(ModelGateway):
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.prompts.append(request.prompt)
        return ModelResponse(text=self.reply, model_id="fake/model:free", usage={})

    async def health_check(self) -> bool:
        return True


# ---------------------------------------------------------------- registry / selection


def test_registry_registers_source_tools_and_parses_setting() -> None:
    reg = default_registry()
    assert set(SOURCE_TOOL_IDS) <= set(reg.list_tools())
    assert all(reg.get_definition(t).cost_class == "FREE" for t in SOURCE_TOOL_IDS)  # type: ignore[union-attr]
    assert parse_source_tools("all") == list(SOURCE_TOOL_IDS)
    assert parse_source_tools("none") == [] and parse_source_tools("") == []
    assert parse_source_tools("reddit_search, bogus,academic_search") == ["reddit_search", "academic_search"]
    assert Settings(live_source_tools="reddit_search").enabled_source_tools() == ["reddit_search"]
    assert list(build_source_tools(["commons_image_search"])) == ["commons_image_search"]


def test_selector_routes_by_modality() -> None:
    selector = ToolSelector(default_registry())
    task = ResearchTask(plan_id=uuid4(), description="Find arxiv papers", objective="scholar articles")
    assert selector.select_tool(task).identifier == "academic_search"  # type: ignore[union-attr]
    task = ResearchTask(plan_id=uuid4(), description="Find reddit discussion", objective="community views")
    assert selector.select_tool(task).identifier == "reddit_search"  # type: ignore[union-attr]


# ---------------------------------------------------------------- planner


def test_route_source_tools_keywords() -> None:
    every = list(SOURCE_TOOL_IDS)
    assert route_source_tools("What do recent studies say about intermittent fasting?", every) == ["academic_search"]
    assert route_source_tools("Reddit opinions on mechanical keyboards", every) == ["reddit_search"]
    assert route_source_tools("Best YouTube lecture on transformers", every) == ["youtube_transcript"]
    assert route_source_tools("What does a quokka look like?", every) == ["commons_image_search"]
    assert route_source_tools("Explain photosynthesis", every) == []
    assert route_source_tools("studies on reddit", ["reddit_search"]) == ["reddit_search"]
    assert route_source_tools("photos of studies on reddit", every) == ["academic_search", "reddit_search"]


def test_deterministic_planner_adds_source_tasks_only_when_enabled() -> None:
    req = ResearchRequest(objective="What do clinical studies say about intermittent fasting?")
    state = ResearchState(request_id=req.id)
    plain = asyncio.run(DeterministicPlanningAgent().create_plan(req, state))
    assert {t.assigned_tool for t in plain.tasks} == {"web_search"}
    plan = asyncio.run(DeterministicPlanningAgent(SOURCE_TOOL_IDS).create_plan(req, state))
    academic = [t for t in plan.tasks if t.assigned_tool == "academic_search"]
    assert len(academic) == 1 and academic[0].metadata["section"] == "Academic literature"


def test_llm_planner_offers_and_validates_source_tools() -> None:
    req = ResearchRequest(objective="Is RAG still useful?")
    reply = json.dumps({"rationale": "r", "tasks": [
        {"section": "Papers", "query": "retrieval augmented generation evaluation", "focus": "f",
         "tool": "academic_search"},
        {"section": "Web", "query": "rag vs long context 2026", "focus": "f"}]})
    llm = PlanLLM(reply)
    plan = asyncio.run(LlmPlanningAgent(llm, source_tools=["academic_search"]).create_plan(
        req, ResearchState(request_id=req.id)))
    assert [t.assigned_tool for t in plan.tasks] == ["academic_search", "web_search"]
    assert "academic_search:" in llm.prompts[0] and "reddit_search" not in llm.prompts[0]
    # A tool that is not enabled is rejected -> deterministic fallback plan.
    bad = PlanLLM(reply.replace("academic_search", "reddit_search"))
    state = ResearchState(request_id=req.id)
    fallback = asyncio.run(LlmPlanningAgent(bad, source_tools=["academic_search"]).create_plan(req, state))
    assert state.metadata["agent_decisions"][-1]["source"] == "deterministic"
    assert all(t.assigned_tool == "web_search" for t in fallback.tasks)
    # Without source tools the prompt is the plain web planner.
    plain = PlanLLM(reply)
    asyncio.run(LlmPlanningAgent(plain).create_plan(req, ResearchState(request_id=req.id)))
    assert '"tool"' not in plain.prompts[0]


# ---------------------------------------------------------------- live agent


def test_live_agent_runs_source_task_with_cited_metadata() -> None:
    agent = live_agent({"academic_search": AcademicSearchTool(http_client=client())})
    task = ResearchTask(plan_id=uuid4(), description="d", objective="o", assigned_tool="academic_search",
                        tool_input={"query": "intermittent fasting trial"}, metadata={"question": "fasting?"})
    result = asyncio.run(agent.execute_task(task))
    assert result.success and result.metadata["source_tool"] == "academic_search"
    source = result.output["source_objects"][0]
    assert str(source.url) == "https://www.semanticscholar.org/paper/f1"
    assert source.metadata["source_type"] == "academic" and source.metadata["year"] == "2022"
    assert result.output["evidence_objects"][0].content == "A randomized trial of intermittent fasting."
    assert result.metadata["provider_errors"] == {"arxiv": "http_503", "openalex": "http_503"}


def test_live_agent_falls_back_to_web_when_source_fails() -> None:
    agent = live_agent({"reddit_search": FailingTool()})
    task = ResearchTask(plan_id=uuid4(), description="d", objective="o", assigned_tool="reddit_search",
                        tool_input={"query": "alpha"})
    result = asyncio.run(agent.execute_task(task))
    assert result.success
    assert result.metadata["source_tool_fallback"] == "http_429"
    assert str(result.output["source_objects"][0].url) == "https://example.org/a"


def test_source_hits_skip_empty_and_bad_urls() -> None:
    sources, evidence = source_hits_to_evidence(
        [{"url": "https://x.org/1", "content": "text", "source_type": "image", "image_url": "https://x.org/i.png"},
         {"url": "https://x.org/2", "content": ""}, {"url": "not a url", "content": "x"}], "commons_image_search", "q")
    assert len(sources) == 1 and evidence[0].content_type == "image_text"
    assert sources[0].metadata["image_url"] == "https://x.org/i.png"


# ---------------------------------------------------------------- citations


def test_citation_rows_and_lines_label_source_type() -> None:
    sources, _ = source_hits_to_evidence([{"url": "https://doi.org/10.1/x", "title": "Paper", "content": "abs",
                                           "source_type": "academic", "authors": "A. Author, B. Author",
                                           "year": "2022", "venue": "NEJM"}], "academic_search", "q")
    row = _source_row(sources[0])
    assert row is not None and row["source_type"] == "academic" and row["tool"] == "academic_search"
    line = _citation_line({"number": 1, "title": "Paper", "url": "https://doi.org/10.1/x", "kind": "academic",
                           "authors": "A. Author, B. Author", "year": "2022", "venue": "NEJM"})
    assert line == "[1] Paper (paper, A. Author et al., 2022, NEJM) - https://doi.org/10.1/x"
    assert _citation_line({"number": 2, "title": "W", "url": "https://w.org"}) == "[2] W - https://w.org"


def test_end_to_end_live_run_cites_academic_source(monkeypatch: pytest.MonkeyPatch) -> None:
    app = ResearchApplication(Settings(research_mode="live", agent_mode="deterministic"))
    monkeypatch.setattr(app, "_build_research_agent",
                        lambda: live_agent({"academic_search": AcademicSearchTool(http_client=client())}))
    resp = asyncio.run(app.submit(ResearchSubmission(objective="What do clinical studies say about fasting?")))
    assert resp.report is not None
    citations = resp.report["citations"]
    academic = [c for c in citations if c.get("kind") == "academic"]
    assert academic and academic[0]["url"] == "https://www.semanticscholar.org/paper/f1"
    assert "(paper, A. Author et al., 2022, NEJM)" in resp.report["content"]
    assert any(c["url"] == "https://example.org/a" for c in citations)


def test_live_application_wires_enabled_source_tools() -> None:
    agent = ResearchApplication(Settings(research_mode="live", live_source_tools="youtube_transcript"))
    built = agent._build_research_agent()
    assert list(built.source_tools) == ["youtube_transcript"]  # type: ignore[attr-defined]
