"""Offline tests: persisted+probed free-model selection, attachment-first synthesis,
image-only PDFs, and concise report headings."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from PIL import Image
from pydantic import SecretStr

from deep_research.core.agents.live_research_agent import LiveResearchAgent
from deep_research.core.agents.planning_agent import DeterministicPlanningAgent, detect_aspects
from deep_research.core.agents.report_agent import DeterministicReportAgent, section_heading
from deep_research.core.attachment_context import attachment_entry, select_attachment_sources
from deep_research.core.config import Settings
from deep_research.core.provider_wiring import resolve_model_cache_path
from deep_research.domain.research.research_state import ResearchState
from deep_research.domain.research.research_task import ResearchTask
from deep_research.domain.research_request import ResearchAttachment, ResearchRequest
from deep_research.models.model_gateway import ModelGateway, ModelRequest, ModelResponse
from deep_research.models.openrouter_catalog import OpenRouterFreeModelCatalog, default_cache_path
from deep_research.tools.pdf_tool import NO_TEXT_NOTE, PdfReaderTool
from deep_research.tools.tool import Tool, ToolRequest, ToolResult
from tests.unit.test_multimodal_tools import make_pdf, pdf_url

KEY = "sk-or-test-SECRET-never-on-disk"
MODELS = {"data": [
    {"id": "slow/text:free", "architecture": {"input_modalities": ["text"]}, "context_length": 900_000},
    {"id": "fast/text:free", "architecture": {"input_modalities": ["text"]}, "context_length": 8_000},
    {"id": "busy/text:free", "architecture": {"input_modalities": ["text"]}, "context_length": 500_000},
    {"id": "vis/a:free", "architecture": {"input_modalities": ["text", "image"]}, "context_length": 100_000},
    {"id": "paid/x", "architecture": {"input_modalities": ["text"]}, "context_length": 1},
]}


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class Api:
    def __init__(self, *, models_status: int = 200) -> None:
        self.models_calls = 0
        self.probed: list[str] = []
        self.models_status = models_status

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            self.models_calls += 1
            return httpx.Response(self.models_status, json=MODELS)
        body = json.loads(request.content)
        model = body["model"]
        self.probed.append(model)
        if model == "busy/text:free":
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        await asyncio.sleep(0.15 if model == "slow/text:free" else 0.0)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


def catalog(api: Api, path: Path, clock: Clock, probe: int = 3) -> OpenRouterFreeModelCatalog:
    return OpenRouterFreeModelCatalog(api_key=SecretStr(KEY), http_client=httpx.AsyncClient(transport=httpx.MockTransport(api)),
                                      cache_path=path, probe_count=probe, probe_timeout=2.0, clock=clock,
                                      cache_ttl_seconds=21_600)


# -- 1) persisted, probed selection -------------------------------------------------

def test_first_run_probes_picks_fastest_and_persists_without_key(tmp_path: Path) -> None:
    path, clock, api = tmp_path / "cache" / "models.json", Clock(), Api()
    cat = catalog(api, path, clock)
    text = run(cat.ranked("text"))
    assert api.models_calls == 1 and set(api.probed) >= {"slow/text:free", "busy/text:free", "fast/text:free"}
    assert cat.selection["text"] == "fast/text:free" and cat.selection["vision"] == "vis/a:free"
    assert text[0] == "fast/text:free" and text[-1] == "busy/text:free"  # probed-failing last
    raw = path.read_text()
    data = json.loads(raw)
    assert KEY not in raw and "SECRET" not in raw and "Bearer" not in raw
    assert data["selection"]["text"] == "fast/text:free" and data["ranked"]["text"][0] == "fast/text:free"
    assert data["probes"]["busy/text:free"]["ok"] is False and data["saved_at"] == clock.now
    assert "paid/x" not in json.dumps(data["models"])


def test_probe_continues_to_next_batch_when_kind_has_no_winner(tmp_path: Path) -> None:
    api = Api()
    cat = catalog(api, tmp_path / "m.json", Clock(), probe=1)
    run(cat.ranked("text"))
    # probe=1: first text candidate (slow, 900k ctx) works -> no 2nd text batch; vision found in batch 1
    assert cat.selection == {"text": "slow/text:free", "vision": "vis/a:free"}

    class BusyFirst(Api):
        async def __call__(self, request: httpx.Request) -> httpx.Response:
            if not request.url.path.endswith("/models") and json.loads(request.content)["model"] == "slow/text:free":
                self.probed.append("slow/text:free")
                return httpx.Response(403, json={"error": {"message": "only available on agentic harnesses"}})
            return await super().__call__(request)

    api2 = BusyFirst()
    cat2 = catalog(api2, tmp_path / "m2.json", Clock(), probe=1)
    run(cat2.ranked("text"))
    assert cat2.selection["text"] == "fast/text:free"  # batch2 busy=429, batch3 fast
    assert api2.probed.count("slow/text:free") == 1 and len(api2.probed) >= 3


def test_restart_reuses_cache_then_rediscovers_after_ttl(tmp_path: Path) -> None:
    path, clock = tmp_path / "models.json", Clock()
    run(catalog(Api(), path, clock).ranked("text"))
    # "restart": new catalog, /models would fail -> must come from disk, no probes
    api2 = Api(models_status=500)
    cat2 = catalog(api2, path, clock)
    assert run(cat2.ranked("text"))[0] == "fast/text:free"
    assert api2.models_calls == 0 and api2.probed == [] and cat2.selection["text"] == "fast/text:free"
    # next day: expired -> re-discover and re-probe
    clock.now += 86_400
    api3 = Api()
    cat3 = catalog(api3, path, clock)
    run(cat3.ranked("text"))
    assert api3.models_calls == 1 and api3.probed
    assert json.loads(path.read_text())["saved_at"] == clock.now


def test_corrupt_or_foreign_cache_ignored(tmp_path: Path) -> None:
    path, clock = tmp_path / "models.json", Clock()
    path.write_text("{not json")
    api = Api()
    run(catalog(api, path, clock, probe=0).ranked("text"))
    assert api.models_calls == 1 and api.probed == []  # probe_count=0 -> no probes
    data = json.loads(path.read_text())
    data["base_url"] = "https://elsewhere.example/api"
    path.write_text(json.dumps(data))
    api2 = Api()
    run(catalog(api2, path, clock, probe=0).ranked("text"))
    assert api2.models_calls == 1


def test_runtime_failure_demotes_even_probed_winner(tmp_path: Path) -> None:
    cat = catalog(Api(), tmp_path / "m.json", Clock())
    run(cat.ranked("text"))
    cat.demote("fast/text:free")
    assert run(cat.ranked("text"))[-1] == "fast/text:free"


def test_cache_path_resolution(tmp_path: Path) -> None:
    assert resolve_model_cache_path(Settings(_env_file=None, openrouter_model_cache_path="off")) is None
    custom = resolve_model_cache_path(Settings(_env_file=None, openrouter_model_cache_path=str(tmp_path / "x.json")))
    assert custom == tmp_path / "x.json"
    assert resolve_model_cache_path(Settings(_env_file=None)) == default_cache_path()
    assert default_cache_path().name == "openrouter_free_models.json"


# -- 3) image-only PDFs ---------------------------------------------------------------

def scanned_pdf() -> bytes:
    img = Image.new("RGB", (120, 80), "white")
    buf = io.BytesIO()
    img.save(buf, format="PDF")
    return buf.getvalue()


class FakeVision(ModelGateway):
    def __init__(self, fail: bool = False) -> None:
        self.requests: list[ModelRequest] = []
        self.fail = fail

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("down")
        return ModelResponse(text="Scanned memo: reactor output 77 MWe.", usage={}, model_id="vis/a:free")

    async def health_check(self) -> bool:
        return True


def pdf_run(tool: PdfReaderTool, raw: bytes) -> ToolResult:
    return run(tool.execute(ToolRequest(tool_name="pdf_reader", parameters={"pdf_data": pdf_url(raw), "source_title": "scan.pdf"})))


def test_image_only_pdf_completes_without_vision() -> None:
    res = pdf_run(PdfReaderTool(), scanned_pdf())
    assert res.success and res.output["note"] == NO_TEXT_NOTE and res.output["has_text"] is False
    assert res.output["page_count"] == 1 and res.output["image_count"] == 1
    img = res.output["images"][0]
    assert (img["width"], img["height"]) == (120, 80) and "data" not in img
    assert "no extractable text" in res.output["summary"] and "not described" in res.output["summary"]


def test_image_only_pdf_described_by_vision_model() -> None:
    vision = FakeVision()
    res = pdf_run(PdfReaderTool(vision_gateway=vision), scanned_pdf())
    assert res.success and res.metadata["model_id"] == "vis/a:free"
    assert "77 MWe" in res.output["summary"] and res.output["image_descriptions"][0]["page"] == 1
    req = vision.requests[0]
    assert req.image_data and req.image_data.startswith("data:image/") and req.parameters["required_capability"] == "vision"


def test_image_only_pdf_vision_failure_still_completes() -> None:
    res = pdf_run(PdfReaderTool(vision_gateway=FakeVision(fail=True)), scanned_pdf())
    assert res.success and "RuntimeError" in res.output["summary"]


def test_text_pdf_exposes_pages() -> None:
    res = pdf_run(PdfReaderTool(), make_pdf(["Alpha page", "Beta page"]))
    assert res.output["has_text"] is True and [p["page"] for p in res.output["pages"]] == [1, 2]


# -- 2) attachments are primary sources ----------------------------------------------

def pdf_output(text_pages: list[str]) -> dict[str, Any]:
    return pdf_run(PdfReaderTool(), make_pdf(text_pages)).output  # type: ignore[no-any-return]


def test_attachment_chunks_ranked_by_relevance() -> None:
    entry = attachment_entry("pdf_reader", pdf_output(["Cooking recipes", "Reactor Zephyr-9 costs 4.2 billion"]))
    assert entry is not None and entry["kind"] == "pdf" and len(entry["chunks"]) == 2
    assert attachment_entry("web_search", {}) is None
    sources, evidence = select_attachment_sources([entry], "zephyr reactor costs", max_chunks=1)
    assert len(sources) == 1 and "Zephyr-9" in evidence[0].content
    assert sources[0].url is None and sources[0].metadata["attachment"] is True and "p.2" in (sources[0].title or "")


class Search(Tool):
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok

    @property
    def name(self) -> str:
        return "web_search"

    def get_definition(self) -> Any:
        raise NotImplementedError

    async def health_check(self) -> bool:
        return True

    async def execute(self, request: ToolRequest) -> ToolResult:
        if not self.ok:
            return ToolResult(success=False, error="blocked", tool_name="web_search", metadata={})
        return ToolResult(success=True, tool_name="web_search", output={"results": [
            {"url": "https://web.example/a", "title": "Web A", "snippet": "web context about reactors"}]})


class Fetch(Search):
    @property
    def name(self) -> str:
        return "web_fetch"

    async def execute(self, request: ToolRequest) -> ToolResult:
        return ToolResult(success=False, error="skip", tool_name="web_fetch")


class Recorder(ModelGateway):
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.prompts.append(request.prompt)
        return ModelResponse(text="Zephyr-9 costs 4.2 billion [1]; context [2].", usage={}, model_id="t/x:free")

    async def health_check(self) -> bool:
        return True


def live_task(entries: list[dict[str, Any]]) -> ResearchTask:
    return ResearchTask(plan_id=uuid4(), description="d", objective="Identify the risks relevant to q",
                        assigned_tool="web_search", tool_input={"query": "risks zephyr", "limit": 5},
                        metadata={"attachment_context": entries, "question": "What does Zephyr-9 cost?",
                                  "section": "Risks", "focus": "the risks only"})


def test_live_agent_puts_attachment_first_in_prompt_and_sources() -> None:
    entry = attachment_entry("pdf_reader", pdf_output(["Reactor Zephyr-9 costs 4.2 billion"]))
    assert entry is not None
    gw = Recorder()
    agent = LiveResearchAgent(search_tool=Search(), fetch_tool=Fetch(), gateway=gw, media_tools={})
    res = run(agent.execute_task(live_task([entry])))
    assert res.success and res.metadata["attachment_sources"] == 1
    prompt = gw.prompts[0]
    assert prompt.index("[1] ") < prompt.index("costs 4.2") < prompt.index("[2] Web A")
    assert "PRIMARY" in prompt and "Focus for this section: the risks only" in prompt
    assert "Objective: What does Zephyr-9 cost?" in prompt
    assert res.output["source_objects"][0].metadata["attachment"] is True


def test_live_agent_uses_attachments_when_web_blocked() -> None:
    entry = attachment_entry("pdf_reader", pdf_output(["Reactor Zephyr-9 costs 4.2 billion"]))
    assert entry is not None
    agent = LiveResearchAgent(search_tool=Search(ok=False), fetch_tool=Fetch(), gateway=Recorder(), media_tools={})
    res = run(agent.execute_task(live_task([entry])))
    assert res.success and res.metadata["search_error"] == "blocked" and len(res.output["source_objects"]) == 1


def test_orchestrated_live_run_cites_attachment_and_handles_scan() -> None:
    """End-to-end through the real orchestrator with stub web + model; PDF fact cited first."""
    from deep_research.api.research_service import ResearchApplication, ResearchSubmission

    gw = Recorder()
    app = ResearchApplication(Settings(_env_file=None, research_mode="live"))
    app._build_research_agent = lambda: LiveResearchAgent(  # type: ignore[method-assign]
        search_tool=Search(), fetch_tool=Fetch(), gateway=gw,
        media_tools={"pdf_reader": PdfReaderTool()})
    sub = ResearchSubmission(objective="What are the cost risks of Zephyr-9?", attachments=[
        ResearchAttachment(kind="pdf", data=pdf_url(make_pdf(["Reactor Zephyr-9 costs 4.2 billion"])), name="z.pdf"),
        ResearchAttachment(kind="pdf", data=pdf_url(scanned_pdf()), name="scan.pdf"),
    ])
    resp = run(app.submit(sub))
    report = resp.report or {}
    assert resp.status == "completed", resp.error
    content = report["content"]
    assert "## Attached files" in content and "no extractable text" in content
    assert report["citations"][0]["kind"] == "attachment" and "z.pdf" in report["citations"][0]["title"]
    assert "4.2 billion [1]" in content and "user attachment" in content
    assert "What are the cost risks" not in "".join(line for line in content.splitlines() if line.startswith("## "))
    assert any(a.get("has_text") is False for a in report["attachments"])


# -- 4) concise headings --------------------------------------------------------------

def test_planner_sections_from_question_keywords() -> None:
    q = "What are the main benefits and risks of small modular nuclear reactors?"
    req = ResearchRequest(objective=q)
    plan = run(DeterministicPlanningAgent().create_plan(req, ResearchState(request_id=req.id)))
    assert [t.metadata["section"] for t in plan.tasks] == ["Fundamentals", "Benefits", "Risks"]
    assert all(q not in t.metadata["section"] for t in plan.tasks)
    assert plan.tasks[1].tool_input["query"].startswith("benefits")
    # no false positive on "construction"/"consider"; generic questions get default aspects
    assert [a[0] for a in detect_aspects("Consider construction of bridges")] == [
        "Recent developments", "Significance and impact"]
    assert [a[0] for a in detect_aspects("solar pros and cons")] == ["Benefits", "Risks"]


def test_section_heading_fallbacks() -> None:
    assert section_heading({"section": "Risks", "objective": "long"}) == "Risks"
    assert section_heading({"objective": "Understand the fundamentals of X?"}) == "Fundamentals"
    assert section_heading({"objective": "a b c d e f g h"}) == "a b c d e f..."
    assert section_heading({}) == "Findings"


def test_report_uses_sections_not_question() -> None:
    state = ResearchState(request_id=uuid4())
    state.metadata["live_findings"] = [{
        "objective": "Understand the fundamentals of What is X?", "section": "Fundamentals", "summary": "X [1].",
        "model_id": "m:free", "sources": [{"source_id": "1", "url": "https://x.example", "title": "X"}]}]
    report = run(DeterministicReportAgent().generate_report(state, {"claims": [], "summary": "s"}))
    assert report["content"].startswith("## Fundamentals\n\nX [1].")


def test_gateway_falls_back_on_null_content_and_error_body() -> None:
    from deep_research.models.providers.openrouter_gateway import OpenRouterModelGateway

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS)
        model = json.loads(request.content)["model"]
        seen.append(model)
        if model == "slow/text:free":
            return httpx.Response(200, json={"choices": [{"message": {"content": None, "reasoning": "..."}}]})
        if model == "busy/text:free":
            return httpx.Response(200, json={"error": {"message": "overloaded", "code": 503}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "fine"}}]})

    gw = OpenRouterModelGateway(api_key=SecretStr("k"), http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    res = run(gw.generate(ModelRequest(prompt="p")))
    assert res.text == "fine" and seen[:2] == ["slow/text:free", "busy/text:free"]


def test_report_normalizes_fullwidth_citations() -> None:
    state = ResearchState(request_id=uuid4())
    state.metadata["live_findings"] = [{
        "section": "Benefits", "summary": "Cuts time to 31\u202fmonths\u30101\u3011\u30102\u3011.", "model_id": "m:free",
        "sources": [{"source_id": "a", "url": None, "title": "k.pdf (attached pdf, p.1)", "kind": "attachment", "page": 1},
                    {"source_id": "b", "url": "https://w.example", "title": "W"}]}]
    report = run(DeterministicReportAgent().generate_report(state, {"claims": [], "summary": "s"}))
    assert "31 months[1, 2]." in report["content"] and "[1] k.pdf (attached pdf, p.1) - user attachment" in report["content"]


def test_attachment_description_headings_flattened() -> None:
    state = ResearchState(request_id=uuid4())
    state.metadata["live_findings"] = [{
        "kind": "attachment", "section": "Attached pdf: s.pdf", "summary": "Intro\n---\n### Transcription\ntext",
        "attachment": {"page_count": 1, "has_text": False, "note": "no extractable text", "image_count": 1},
        "sources": [{"source_id": "a", "url": None, "title": "s.pdf", "kind": "attachment"}]}]
    content = run(DeterministicReportAgent().generate_report(state, {"claims": [], "summary": "s"}))["content"]
    assert "**Transcription**" in content and "###" not in content and "\n---" not in content
