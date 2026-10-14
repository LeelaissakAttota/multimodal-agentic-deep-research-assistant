"""Offline tests for PDF/image tools, vision wiring and attachment routing."""

from __future__ import annotations

import asyncio
import base64
import io
import json
from typing import Any

import httpx
import pytest
from PIL import Image
from pydantic import SecretStr
from pypdf import PdfWriter

from deep_research.api.research_service import ResearchApplication, ResearchSubmission
from deep_research.core.agents.deterministic_research_agent import DeterministicResearchAgent
from deep_research.core.agents.live_research_agent import LiveResearchAgent
from deep_research.core.agents.planning_agent import DeterministicPlanningAgent
from deep_research.core.config import Settings
from deep_research.core.free_model_guard import FreeModelGuard, ModelCapability
from deep_research.core.provider_wiring import build_provider_gateway
from deep_research.domain.research.research_state import ResearchState
from deep_research.domain.research_request import ResearchAttachment, ResearchRequest
from deep_research.errors.base import ModelError
from deep_research.models.guarded_gateway import GuardedModelGateway
from deep_research.models.model_gateway import ModelGateway, ModelRequest, ModelResponse
from deep_research.models.providers.openrouter_gateway import OpenRouterModelGateway
from deep_research.tools.image_tool import ImageAnalysisTool
from deep_research.tools.pdf_tool import PdfReaderTool
from deep_research.tools.tool import Tool, ToolRequest, ToolResult


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# -- sample file generators --------------------------------------------------

def make_pdf(pages: list[str]) -> bytes:
    """Build a minimal valid text PDF with one Helvetica text line per page."""
    objs: list[bytes] = []
    n = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    font_id = 3 + 2 * n
    for i, text in enumerate(pages):
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * i} 0 R "
                    f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for num, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % num + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def pdf_url(raw: bytes) -> str:
    return "data:application/pdf;base64," + base64.b64encode(raw).decode()


def make_image(fmt: str = "PNG", size: tuple[int, int] = (32, 16)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buf, format=fmt)
    return buf.getvalue()


def img_url(raw: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


class FakeVision(ModelGateway):
    def __init__(self, result: Any = "A red rectangle.") -> None:
        self.result = result
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if isinstance(self.result, BaseException):
            raise self.result
        return ModelResponse(text=self.result, model_id="google/gemma-3-27b-it:free")

    async def health_check(self) -> bool:
        return True


def pdf_exec(tool: PdfReaderTool, **params: Any) -> ToolResult:
    return run(tool.execute(ToolRequest(tool_name="pdf_reader", parameters=params)))


def img_exec(tool: ImageAnalysisTool, **params: Any) -> ToolResult:
    return run(tool.execute(ToolRequest(tool_name="image_analyzer", parameters=params)))


# -- PDF ---------------------------------------------------------------------

def test_pdf_extracts_text_with_provenance() -> None:
    res = pdf_exec(PdfReaderTool(), pdf_data=pdf_url(make_pdf(["Hello alpha", "Second beta"])), source_title="doc")
    assert res.success, res.error
    out = res.output
    assert "Hello alpha" in out["text"] and "Second beta" in out["text"]
    assert out["page_count"] == 2 and out["pages_read"] == 2 and not out["truncated"]
    assert out["evidence_objects"][0].content_type == "pdf_text"
    assert out["source_objects"][0].title == "doc"


def test_pdf_page_and_char_limits() -> None:
    raw = make_pdf([f"Page {i} text" for i in range(5)])
    res = pdf_exec(PdfReaderTool(max_pages=2), pdf_data=pdf_url(raw))
    assert res.output["pages_read"] == 2 and res.output["truncated"] and "Page 3" not in res.output["text"]
    res = pdf_exec(PdfReaderTool(), pdf_data=pdf_url(raw), max_pages=1)
    assert res.output["pages_read"] == 1
    res = pdf_exec(PdfReaderTool(max_chars=8), pdf_data=pdf_url(raw))
    assert len(res.output["text"]) == 8 and res.output["truncated"]


@pytest.mark.parametrize(
    "data,kind",
    [
        ("", "missing_input"),
        ("https://x/a.pdf", "unsupported_representation"),
        ("data:text/plain;base64,aGk=", "unsupported_type"),
        ("data:application/pdf;base64,***", "invalid_base64"),
        (pdf_url(b"not a pdf at all"), "invalid_pdf"),
        (pdf_url(b"%PDF-1.4\ngarbage"), "invalid_pdf"),
    ],
)
def test_pdf_input_errors(data: str, kind: str) -> None:
    res = pdf_exec(PdfReaderTool(), pdf_data=data)
    assert not res.success and res.metadata["failure_kind"] == kind


def test_pdf_size_limit() -> None:
    res = pdf_exec(PdfReaderTool(max_bytes=100), pdf_data=pdf_url(make_pdf(["x" * 50, "y" * 50])))
    assert res.metadata["failure_kind"] == "too_large"


def test_pdf_encrypted_and_no_text() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    blank = io.BytesIO()
    writer.write(blank)
    assert pdf_exec(PdfReaderTool(), pdf_data=pdf_url(blank.getvalue())).metadata["failure_kind"] == "no_text"
    writer.encrypt("secret")
    enc = io.BytesIO()
    writer.write(enc)
    assert pdf_exec(PdfReaderTool(), pdf_data=pdf_url(enc.getvalue())).metadata["failure_kind"] == "encrypted_pdf"


# -- Image -------------------------------------------------------------------

@pytest.mark.parametrize("fmt,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp"),
                                      ("GIF", "image/gif")])
def test_image_metadata_offline(fmt: str, mime: str) -> None:
    res = img_exec(ImageAnalysisTool(use_settings=False), image_data=img_url(make_image(fmt), mime))
    assert res.success, res.error
    assert (res.output["width"], res.output["height"], res.output["format"]) == (32, 16, fmt)
    assert res.output["description"] is None and res.metadata["description_source"] == "metadata_only"
    assert res.output["evidence_objects"][0].content_type == "image_metadata"


def test_image_vision_description() -> None:
    vision = FakeVision()
    res = img_exec(ImageAnalysisTool(vision), image_data=img_url(make_image()), prompt="What is shown?")
    assert res.output["description"] == "A red rectangle." and res.metadata["description_source"] == "vision_model"
    req = vision.requests[0]
    assert req.image_data and req.image_data.startswith("data:image/png;base64,")
    assert req.parameters["required_capability"] == "vision" and req.prompt == "What is shown?"


@pytest.mark.parametrize("err", [ModelError("x", error_code="paid_model_rejected"), RuntimeError("x"), "   "])
def test_image_vision_failure_falls_back(err: Any) -> None:
    res = img_exec(ImageAnalysisTool(FakeVision(err)), image_data=img_url(make_image()))
    assert res.success and res.output["description"] is None and res.metadata["vision_error"]


@pytest.mark.parametrize(
    "data,kind",
    [
        ("", "missing_input"),
        ("data:image/svg+xml;base64,PHN2Zz4=", "unsupported_type"),
        (img_url(b"not an image"), "invalid_image"),
        (img_url(make_image("JPEG"), "image/png"), "type_mismatch"),
    ],
)
def test_image_input_errors(data: str, kind: str) -> None:
    vision = FakeVision()
    res = img_exec(ImageAnalysisTool(vision), image_data=data)
    assert not res.success and res.metadata["failure_kind"] == kind and vision.requests == []


def test_image_size_and_pixel_limits() -> None:
    raw = make_image(size=(200, 200))
    assert img_exec(ImageAnalysisTool(use_settings=False, max_bytes=50),
                    image_data=img_url(raw)).metadata["failure_kind"] == "too_large"
    assert img_exec(ImageAnalysisTool(use_settings=False, max_pixels=1000),
                    image_data=img_url(raw)).metadata["failure_kind"] == "too_large"


# -- vision wiring / guard ---------------------------------------------------

def test_vision_gateway_uses_free_vision_model_and_sends_image() -> None:
    sent: list[dict[str, Any]] = []

    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [
                {"id": "text/only:free", "architecture": {"input_modalities": ["text"]}, "context_length": 9},
                {"id": "vis/model:free", "architecture": {"input_modalities": ["text", "image"]},
                 "context_length": 8},
            ]})
        sent.append(json.loads(r.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "desc"}}]})

    s = Settings(_env_file=None, openrouter_api_key=SecretStr("k"))
    assert s.openrouter_vision_model == "auto"
    g = build_provider_gateway(s, capability=ModelCapability.VISION)
    assert isinstance(g, GuardedModelGateway) and g.model_id == "auto:free"
    inner = OpenRouterModelGateway(api_key=SecretStr("k"), model=g.model_id,
                                   http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    guarded = GuardedModelGateway(inner, model_id=g.model_id, provider="openrouter",
                                  capability=ModelCapability.VISION, guard=FreeModelGuard())
    res = img_exec(ImageAnalysisTool(guarded), image_data=img_url(make_image()))
    assert res.output["description"] == "desc"
    assert sent[0]["model"] == "vis/model:free"
    content = sent[0]["messages"][0]["content"]
    assert content[1]["type"] == "image_url" and content[1]["image_url"]["url"].startswith("data:image/png")


def test_paid_vision_model_rejected_before_call() -> None:
    s = Settings(_env_file=None, openrouter_api_key=SecretStr("k"), openrouter_vision_model="openai/gpt-4o")
    g = build_provider_gateway(s, capability=ModelCapability.VISION)
    res = img_exec(ImageAnalysisTool(g), image_data=img_url(make_image()))
    assert res.success and res.metadata["vision_error"] == "paid_model_rejected"


# -- registry / selector / planning / flow -----------------------------------

def test_registry_and_selector_include_multimodal_tools() -> None:
    agent = DeterministicResearchAgent()
    assert {"pdf_reader", "image_analyzer"} <= set(agent.registry.list_tools())
    from deep_research.domain.research.research_task import ResearchTask
    from uuid import uuid4
    pick = agent.selector.select_tool(ResearchTask(plan_id=uuid4(), description="describe the photo",
                                                   objective="describe image"))
    assert pick is not None and pick.identifier == "image_analyzer"


def test_planner_adds_attachment_tasks() -> None:
    req = ResearchRequest(objective="solar", attachments=[
        ResearchAttachment(kind="pdf", data=pdf_url(make_pdf(["x"])), name="paper.pdf"),
        ResearchAttachment(kind="image", data=img_url(make_image())),
    ])
    plan = run(DeterministicPlanningAgent().create_plan(req, ResearchState(request_id=req.id)))
    tools = [t.assigned_tool for t in plan.tasks]
    assert tools[-2:] == ["pdf_reader", "image_analyzer"]
    assert "pdf_data" in plan.tasks[-2].tool_input and plan.tasks[-2].tool_input["source_title"] == "paper.pdf"


def test_offline_flow_processes_attachments() -> None:
    app = ResearchApplication(Settings(_env_file=None))
    sub = ResearchSubmission(objective="solar power", attachments=[
        ResearchAttachment(kind="pdf", data=pdf_url(make_pdf(["Solar panels convert light"])), name="p.pdf"),
        ResearchAttachment(kind="image", data=img_url(make_image()), name="i.png"),
    ])
    resp = run(app.submit(sub))
    assert resp.error is None, resp.error
    state = app._states[resp.session_id]
    assert state.failed_task_ids == []
    assert len(state.completed_task_ids) >= 5  # 3 web tasks + pdf + image
    assert len(state.gathered_evidence) >= 2


class _Stub(Tool):
    def __init__(self, tid: str) -> None:
        self.tid = tid
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return self.tid

    def get_definition(self) -> Any:
        raise NotImplementedError

    async def health_check(self) -> bool:
        return True

    async def execute(self, request: ToolRequest) -> ToolResult:
        self.calls.append(request.parameters)
        return ToolResult(success=True, output={"summary": "ok"}, tool_name=self.tid)


def test_live_agent_routes_media_tasks() -> None:
    from deep_research.domain.research.research_task import ResearchTask
    from uuid import uuid4
    search, fetch, pdf = _Stub("s"), _Stub("f"), PdfReaderTool()
    agent = LiveResearchAgent(search_tool=search, fetch_tool=fetch, media_tools={"pdf_reader": pdf})
    t = ResearchTask(plan_id=uuid4(), description="d", objective="o", assigned_tool="pdf_reader",
                     tool_input={"pdf_data": pdf_url(make_pdf(["Live pdf"]))})
    res = run(agent.execute_task(t))
    assert res.success and "Live pdf" in res.output["text"] and search.calls == []


def test_submission_limits_attachments() -> None:
    att = ResearchAttachment(kind="image", data=img_url(make_image()))
    with pytest.raises(ValueError):
        ResearchSubmission(objective="x", attachments=[att] * 6)
