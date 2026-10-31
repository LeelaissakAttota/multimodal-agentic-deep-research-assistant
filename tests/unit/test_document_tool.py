import asyncio
import base64
import io
import zipfile

import pytest

from deep_research.tools.document_tool import (
    DOCX_MIME, PPTX_MIME, DocumentReaderTool, docx_pages, pptx_pages, sniff_format, text_pages,
)
from deep_research.tools.tool import ToolRequest

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    return buf.getvalue()


def make_docx() -> bytes:
    body = (f'<w:document {W}><w:body><w:p><w:r><w:t>Coral reefs</w:t></w:r><w:r><w:t> bleach above 1C.</w:t></w:r></w:p>'
            '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Year</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>2024</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
            '</w:body></w:document>')
    return _zip({"[Content_Types].xml": "<Types/>", "word/document.xml": body})


def make_pptx() -> bytes:
    def slide(t: str) -> str:
        return (f'<p:sld {A}><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>{t}</a:t></a:r></a:p>'
                '</p:txBody></p:sp></p:spTree></p:cSld></p:sld>')
    return _zip({"ppt/presentation.xml": "<x/>", "ppt/slides/slide2.xml": slide("Second slide"),
                 "ppt/slides/slide1.xml": slide("Heatwaves"), "ppt/slides/slide10.xml": slide("Tenth"),
                 "ppt/notesSlides/notesSlide1.xml": f'<p:notes {A}><a:t>Say this</a:t></p:notes>'})


def _run(data: bytes, mime: str) -> object:
    url = f"data:{mime};base64,{base64.b64encode(data).decode()}"
    return asyncio.run(DocumentReaderTool().execute(ToolRequest(tool_name="document_parser",
                                                                parameters={"document_data": url, "source_title": "f"})))


def test_docx_paragraphs_and_tables() -> None:
    pages = docx_pages(make_docx())
    assert pages == [{"page": 1, "text": "Coral reefs bleach above 1C.\nYear | 2024"}]


def test_pptx_slide_order_and_notes() -> None:
    pages = pptx_pages(make_pptx())
    assert [p["text"].split("\n")[0] for p in pages] == ["Heatwaves", "Second slide", "Tenth"]
    assert "Speaker notes: Say this" in pages[0]["text"]


def test_text_and_sniffing() -> None:
    assert text_pages(b"# Title\n\nPara one.\n\nPara two.")[0]["text"] == "# Title\nPara one.\nPara two."
    assert sniff_format(make_docx(), "application/octet-stream") == "docx"
    assert sniff_format(make_pptx(), PPTX_MIME) == "pptx"
    assert sniff_format(b"hello", "text/markdown") == "md"
    with pytest.raises(ValueError):
        sniff_format(b"hello", DOCX_MIME)
    with pytest.raises(ValueError):
        text_pages(b"a\x00b")


def test_tool_outputs_pages_and_sources() -> None:
    res = _run(make_pptx(), PPTX_MIME)
    assert res.success and res.output["format"] == "pptx" and res.output["page_count"] == 3  # type: ignore[attr-defined]
    assert res.output["source_objects"][0].title == "f"  # type: ignore[attr-defined]
    bad = _run(_zip({"a.txt": "x"}), "application/zip")
    assert not bad.success and "not a DOCX or PPTX" in str(bad.error)  # type: ignore[attr-defined]
    dtd = _run(_zip({"word/document.xml": '<!DOCTYPE x [<!ENTITY a "b">]><x/>'}), DOCX_MIME)
    assert not dtd.success  # type: ignore[attr-defined]


def test_offline_run_cites_attached_docx() -> None:
    from deep_research.api.research_service import ResearchApplication, ResearchSubmission
    from deep_research.core.config import Settings

    url = f"data:{DOCX_MIME};base64,{base64.b64encode(make_docx()).decode()}"
    sub = ResearchSubmission(objective="When do coral reefs bleach?",
                             attachments=[{"kind": "document", "data": url, "name": "reefs.docx"}])  # type: ignore[list-item]
    resp = asyncio.run(ResearchApplication(Settings(research_mode="offline")).submit(sub))
    assert resp.status == "completed"


def test_dashboard_accepts_office_uploads() -> None:
    from deep_research.dashboard.core import build_attachment

    att = build_attachment("deck.pptx", make_pptx())
    assert att["kind"] == "document" and att["data"].startswith(f"data:{PPTX_MIME};base64,")
    with pytest.raises(ValueError):
        build_attachment("x.exe", b"MZ")
