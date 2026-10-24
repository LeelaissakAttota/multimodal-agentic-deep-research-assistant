import io
import zipfile

import pytest

from deep_research.export import FORMATS, export_filename, export_report, parse, to_markdown

REPORT = {
    "title": "Research Report: Do coral reefs recover?",
    "content": "## Findings\n\nReefs **can** recover in 10-15 years [1].\n\n- Bleaching 2024 [2]\n\n"
               "| Area | Effect |\n|---|---|\n| GBR | severe [1] |\n\n## Sources\n\n1. x\n",
    "citations": [{"number": 1, "url": "https://www.noaa.gov/a", "title": "NOAA"},
                  {"number": 2, "url": "https://example.org/b", "title": "Blog"}],
    "claim_scores": [{"text": "Reefs can recover in 10-15 years [1].", "confidence": 0.82}],
    "models_used": ["free/model:free"],
}


def test_parse_blocks_tables_and_strips_sources_section() -> None:
    doc = parse(REPORT)
    kinds = [b.kind for b in doc.blocks]
    assert kinds == ["h2", "p", "li", "table"]
    assert doc.blocks[1].text == "Reefs can recover in 10-15 years [1]."
    assert doc.blocks[3].rows == [["Area", "Effect"], ["GBR", "severe [1]"]]
    assert len(doc.sources) == 2 and doc.claims


@pytest.mark.parametrize("fmt", ["pdf", "docx", "pptx"])
def test_binary_exports_are_valid_files(fmt: str) -> None:
    pytest.importorskip({"pdf": "fpdf", "docx": "docx", "pptx": "pptx"}[fmt])
    data = export_report(REPORT, fmt)
    if fmt == "pdf":
        assert data.startswith(b"%PDF-") and len(data) > 1000
    else:
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        assert ("word/document.xml" if fmt == "docx" else "ppt/presentation.xml") in names


def test_docx_and_pptx_contain_report_text() -> None:
    docx = pytest.importorskip("docx")
    pptx = pytest.importorskip("pptx")
    d = docx.Document(io.BytesIO(export_report(REPORT, "docx")))
    text = "\n".join(p.text for p in d.paragraphs)
    assert "Reefs can recover" in text and "NOAA - https://www.noaa.gov/a" in text
    prs = pptx.Presentation(io.BytesIO(export_report(REPORT, "pptx")))
    slide_text = " ".join(sh.text_frame.text for s in prs.slides for sh in s.shapes if sh.has_text_frame)
    assert "Do coral reefs recover" in slide_text and "82%" in slide_text


def test_json_markdown_and_names() -> None:
    assert b'"title"' in export_report(REPORT, "json")
    md = to_markdown(REPORT)
    assert md.startswith("# Research Report") and "82% - Reefs" in md
    assert export_filename(REPORT, "pdf", "abcdef123456") == "do-coral-reefs-recover-abcdef12.pdf"
    assert set(FORMATS) == {"pdf", "docx", "pptx", "json", "md"}
    with pytest.raises(ValueError):
        export_report(REPORT, "exe")
