import asyncio
import base64
import io

import pytest
from PIL import Image, ImageDraw, ImageFont

from deep_research.tools import ocr
from deep_research.tools.tool import ToolRequest

HAVE_OCR = ocr.engine_name() is not None


def text_png(text: str = "Coral bleaching 2024") -> bytes:
    img = Image.new("RGB", (900, 160), "white")
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 56)
    except OSError:
        font = ImageFont.load_default(size=56)
    ImageDraw.Draw(img).text((30, 40), text, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def scanned_pdf() -> bytes:
    fpdf = pytest.importorskip("fpdf")
    pdf = fpdf.FPDF()
    pdf.add_page()
    pdf.image(io.BytesIO(text_png()), x=10, y=10, w=180)
    return bytes(pdf.output())


def test_reading_order_groups_lines() -> None:
    boxes = [([[200, 0], [300, 0], [300, 20], [200, 20]], "world", 0.9),
             ([[0, 2], [100, 2], [100, 22], [0, 22]], "hello", 0.9),
             ([[0, 50], [100, 50], [100, 70], [0, 70]], "next", 0.9),
             ([[0, 90], [100, 90], [100, 110], [0, 110]], "junk", 0.1)]
    assert ocr._order(boxes) == ["hello world", "next"]


def test_disabled_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEP_RESEARCH_OCR", "off")
    ocr.reset_for_tests()
    try:
        assert ocr.engine_name() is None and ocr.ocr_image(text_png()) is None
    finally:
        monkeypatch.delenv("DEEP_RESEARCH_OCR")
        ocr.reset_for_tests()


def test_garbage_bytes_return_none() -> None:
    assert ocr.ocr_image(b"not an image") is None


@pytest.mark.skipif(not HAVE_OCR, reason="no OCR engine installed")
def test_image_tool_adds_ocr_text() -> None:
    from deep_research.tools.image_tool import ImageAnalysisTool

    url = "data:image/png;base64," + base64.b64encode(text_png()).decode()
    res = asyncio.run(ImageAnalysisTool(use_settings=False).execute(
        ToolRequest(tool_name="image_analyzer", parameters={"image_data": url})))
    assert res.success
    assert "bleaching" in (res.output["ocr_text"] or "").lower()
    assert "Text found in the image" in res.output["summary"]
    assert res.metadata["description_source"] == "ocr"


@pytest.mark.skipif(not HAVE_OCR, reason="no OCR engine installed")
def test_scanned_pdf_gets_ocr_pages() -> None:
    from deep_research.tools.pdf_tool import PdfReaderTool

    url = "data:application/pdf;base64," + base64.b64encode(scanned_pdf()).decode()
    res = asyncio.run(PdfReaderTool().execute(
        ToolRequest(tool_name="pdf_reader", parameters={"pdf_data": url})))
    assert res.success and res.output["has_text"] is False
    assert res.output["pages"][0]["page"] == 1
    assert "bleaching" in res.output["pages"][0]["text"].lower()
    assert res.output["ocr_engine"] == ocr.engine_name()


@pytest.mark.skipif(not HAVE_OCR, reason="no OCR engine installed")
def test_offline_report_lists_ocr_image_attachment() -> None:
    from deep_research.api.research_service import ResearchApplication, ResearchSubmission
    from deep_research.core.config import Settings
    from deep_research.domain.research_request import ResearchAttachment

    att = ResearchAttachment(kind="image", data="data:image/png;base64," + base64.b64encode(text_png()).decode(),
                             name="chart.png")
    resp = asyncio.run(ResearchApplication(Settings(research_mode="offline")).submit(
        ResearchSubmission(objective="When do coral reefs bleach?", attachments=[att])))
    content = resp.report["content"]
    assert "Attached image: chart.png" in content and "local OCR" in content and "bleaching" in content.lower()
