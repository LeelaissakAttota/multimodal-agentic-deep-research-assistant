"""Export a finished research report to Markdown, JSON, PDF, DOCX or PPTX.

All renderers take the report dict produced by the report agent (``title``,
``content`` markdown with ``[n]`` citation markers, ``citations``) and return
bytes. PDF/DOCX/PPTX use the optional ``export`` extra (fpdf2, python-docx,
python-pptx); a missing library raises :class:`ExportUnavailable` with the pip
command instead of failing obscurely.
"""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FORMATS: dict[str, tuple[str, str]] = {
    "pdf": ("application/pdf", "pdf"),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
    "pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", "pptx"),
    "json": ("application/json", "json"),
    "md": ("text/markdown", "md"),
    "html": ("text/html", "html"),
}
_INSTALL = 'pip install "multimodal_agentic_deep_research_assistant[export]"'


class ExportUnavailable(RuntimeError):
    """The library for this format is not installed."""


@dataclass
class Block:
    kind: str  # "h1" | "h2" | "h3" | "p" | "li" | "table"
    text: str
    rows: list[list[str]] = field(default_factory=list)


@dataclass
class Doc:
    title: str
    blocks: list[Block] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    claims: list[dict[str, Any]] = field(default_factory=list)
    meta: list[str] = field(default_factory=list)


def _plain(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), text)
    text = re.sub(r"(?<!\w)[*_](\S[^*_]*?)[*_](?!\w)", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 (\2)", text)
    return " ".join(text.split())


def parse(report: dict[str, Any]) -> Doc:  # noqa: C901 - one linear markdown scan
    """Markdown report -> neutral block list; the trailing '## Sources' section becomes ``sources``."""

    doc = Doc(title=_plain(str(report.get("title") or "Research report")))
    content = str(report.get("content") or "")
    content = re.split(r"\n#{1,3}\s*Sources\s*\n", "\n" + content, maxsplit=1)[0]
    para: list[str] = []
    table: list[list[str]] = []

    def flush() -> None:
        if para:
            doc.blocks.append(Block("p", _plain(" ".join(para))))
            para.clear()
        if table:
            width = max(len(r) for r in table)
            doc.blocks.append(Block("table", "", [r + [""] * (width - len(r)) for r in table]))
            table.clear()

    for raw in content.splitlines():
        line = raw.strip()
        heading = re.match(r"^(#{1,3})\s+(.*)$", line)
        bullet = re.match(r"^(?:[-*\u2022]|\d+[.)])\s+(.*)$", line)
        if line.startswith("|") and line.count("|") >= 2:
            if para:
                doc.blocks.append(Block("p", _plain(" ".join(para))))
                para.clear()
            cells = [_plain(c) for c in line.strip("|").split("|")]
            if not all(re.fullmatch(r":?-{2,}:?", c.replace(" ", "")) for c in cells if c):
                table.append(cells)
            continue
        if table:
            flush()
        if not line:
            flush()
        elif heading:
            flush()
            doc.blocks.append(Block(f"h{len(heading.group(1))}", _plain(heading.group(2))))
        elif bullet:
            flush()
            doc.blocks.append(Block("li", _plain(bullet.group(1))))
        else:
            para.append(line)
    flush()
    cites = report.get("citations") or report.get("sources") or []
    doc.sources = [c for c in cites if isinstance(c, dict)]
    if "claim_scores" not in report and doc.sources:
        from deep_research.evidence.scoring import claim_scores, score_citations

        score_citations(doc.sources)
        report = {**report, "claim_scores": claim_scores(str(report.get("content") or ""), doc.sources)}
    doc.claims = [c for c in report.get("claim_scores") or [] if isinstance(c, dict)]
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    models = ", ".join(str(m) for m in report.get("models_used") or [])
    doc.meta = [f"Generated {stamp} by Multimodal Agentic Deep Research Assistant"]
    if models:
        doc.meta.append(f"Models (free tier): {models}")
    return doc


def source_line(src: dict[str, Any]) -> str:
    title = _plain(str(src.get("title") or src.get("url") or "Untitled source"))
    url = str(src.get("url") or "")
    cred = src.get("credibility") if isinstance(src.get("credibility"), dict) else None
    tail = f" (credibility: {cred.get('label')} {cred.get('score')})" if cred else ""
    return f"[{src.get('number', '?')}] {title}" + (f" - {url}" if url else "") + tail


def claim_line(claim: dict[str, Any]) -> str:
    from deep_research.evidence.scoring import clean_claim_text

    label = f" {claim['label']}" if claim.get("label") else ""
    cites = "".join(f"[{n}]" for n in claim.get("citations") or [])
    text = clean_claim_text(str(claim.get("text", ""))) if cites else str(claim.get("text", ""))
    return f"{int(round(float(claim.get('confidence', 0)) * 100))}%{label} - {_plain(text)}{' ' + cites if cites else ''}"


def export_report(report: dict[str, Any], fmt: str) -> bytes:
    fmt = fmt.lower().lstrip(".")
    if fmt not in FORMATS:
        raise ValueError(f"unsupported export format {fmt!r}; use one of {', '.join(FORMATS)}")
    if fmt == "json":
        return json.dumps(report, indent=2, ensure_ascii=False, default=str).encode("utf-8")
    if fmt == "md":
        return to_markdown(report).encode("utf-8")
    if fmt == "html":
        from deep_research.assistant.share import render_html

        return render_html(report).encode("utf-8")
    renderer = {"pdf": _pdf, "docx": _docx, "pptx": _pptx}[fmt]
    return renderer(parse(report))


def block_text(block: Block) -> str:
    return block.text if block.kind != "table" else "\n".join(" | ".join(r) for r in block.rows)


def to_markdown(report: dict[str, Any]) -> str:
    doc = parse(report)
    out = [f"# {doc.title}", ""]
    out += [f"_{m}_" for m in doc.meta] + [""]
    out.append(str(report.get("content") or "").strip())
    if doc.claims:
        out += ["", "## Claim confidence", ""] + [f"- {claim_line(c)}" for c in doc.claims]
    return "\n".join(out).strip() + "\n"


def export_filename(report: dict[str, Any], fmt: str, run_id: str | None = None) -> str:
    stem = re.sub(r"[^A-Za-z0-9]+", "-", str(report.get("title") or "report")).strip("-").lower()[:60] or "report"
    stem = stem.removeprefix("research-report-") or stem
    return f"{stem}{'-' + run_id[:8] if run_id else ''}.{FORMATS[fmt][1]}"


# ---------------------------------------------------------------- PDF
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
)
_BOLD = {"DejaVuSans.ttf": "DejaVuSans-Bold.ttf", "arial.ttf": "arialbd.ttf", "segoeui.ttf": "segoeuib.ttf"}


# Emoji are missing from normal text fonts (fpdf2 prints '?'); map the ones reports use to text labels
# and drop any other pictographs, so PDFs never show '?' boxes.
_EMOJI_LABELS = {
    "\u2705": "[OK]", "\u2714": "[OK]", "\u2611": "[OK]", "\U0001f7e2": "[OK]", "\U0001f7e1": "[~]", "\U0001f7e0": "[~]",
    "\u26aa": "[?]", "\u2753": "[?]", "\u2754": "[?]", "\u274c": "[X]", "\u2716": "[X]", "\U0001f534": "[X]",
    "\u26a0": "[!]", "\u2757": "[!]", "\U0001f4a1": "*", "\U0001f50d": "", "\U0001f4c4": "", "\U0001f310": "",
    "\u2b50": "*", "\u2192": "->", "\u2190": "<-",
}
_PICTOGRAPH = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0E\uFE0F\u200D]")


def pdf_safe_text(s: str) -> str:
    """Replace emoji with short text labels and strip other pictographs (keeps all normal Unicode text)."""
    for emoji, label in _EMOJI_LABELS.items():
        s = s.replace(emoji, label)
    return re.sub(r"[ \t]{2,}", " ", _PICTOGRAPH.sub("", s))


def _font() -> tuple[str | None, str | None]:
    for cand in _FONT_CANDIDATES:
        path = Path(cand)
        if path.is_file():
            bold = path.with_name(_BOLD.get(path.name, path.name))
            return str(path), str(bold if bold.is_file() else path)
    return None, None


def _pdf(doc: Doc) -> bytes:  # noqa: C901 - flat renderer
    try:
        from fpdf import FPDF
    except ImportError as exc:
        raise ExportUnavailable(f"PDF export needs fpdf2: {_INSTALL}") from exc
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(True, margin=16)
    pdf.set_margins(18, 18, 18)
    regular, bold = _font()
    family = "Helvetica"
    if regular:
        pdf.add_font("Body", "", regular)
        pdf.add_font("Body", "B", bold or regular)
        family = "Body"

    def txt(s: str) -> str:
        s = pdf_safe_text(s)
        return s if regular else s.encode("latin-1", "replace").decode("latin-1")

    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin

    def write(s: str, size: float, style: str = "", gap: float = 2.0, color: tuple[int, int, int] = (20, 24, 33)) -> None:
        pdf.set_font(family, style, size)
        pdf.set_text_color(*color)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(width, size * 0.5, txt(s), align="L")
        pdf.ln(gap)

    write(doc.title, 18, "B", 3)
    for m in doc.meta:
        write(m, 8.5, "", 0.5, (100, 110, 125))
    pdf.ln(4)
    sizes = {"h1": 15.0, "h2": 13.0, "h3": 11.5}
    for block in doc.blocks:
        if block.kind in sizes:
            pdf.ln(2)
            write(block.text, sizes[block.kind], "B", 1.5, (30, 64, 175))
        elif block.kind == "table":
            pdf.set_font(family, "", 8.5)
            pdf.set_text_color(20, 24, 33)
            with pdf.table(text_align="LEFT", line_height=4.2, first_row_as_headings=True) as tbl:
                for row in block.rows:
                    cells = tbl.row()
                    for cell in row:
                        cells.cell(txt(cell))
            pdf.ln(3)
        elif block.kind == "li":
            write("\u2022 " + block.text if regular else "- " + block.text, 10.5, "", 1.0)
        else:
            write(block.text, 10.5, "", 2.5)
    if doc.claims:
        pdf.ln(2)
        write("Claim confidence", 13, "B", 1.5, (30, 64, 175))
        for claim in doc.claims:
            write(claim_line(claim), 9.5, "", 1.0)
    if doc.sources:
        pdf.ln(2)
        write("Sources", 13, "B", 1.5, (30, 64, 175))
        for src in doc.sources:
            write(source_line(src), 9, "", 1.0, (55, 65, 81))
    return bytes(pdf.output())


# ---------------------------------------------------------------- DOCX
def _docx(doc: Doc) -> bytes:  # noqa: C901 - flat renderer
    try:
        import docx
    except ImportError as exc:
        raise ExportUnavailable(f"DOCX export needs python-docx: {_INSTALL}") from exc
    out = docx.Document()
    out.add_heading(doc.title, level=0)
    for m in doc.meta:
        out.add_paragraph().add_run(m).italic = True
    for block in doc.blocks:
        if block.kind.startswith("h"):
            out.add_heading(block.text, level=min(int(block.kind[1]), 3))
        elif block.kind == "table":
            tbl = out.add_table(rows=0, cols=len(block.rows[0]))
            tbl.style = "Light Grid Accent 1"
            for row in block.rows:
                for cell, value in zip(tbl.add_row().cells, row):
                    cell.text = value
        elif block.kind == "li":
            out.add_paragraph(block.text, style="List Bullet")
        else:
            out.add_paragraph(block.text)
    if doc.claims:
        out.add_heading("Claim confidence", level=1)
        table = out.add_table(rows=1, cols=2)
        table.style = "Light Grid Accent 1"
        table.rows[0].cells[0].text, table.rows[0].cells[1].text = "Confidence", "Claim"
        for claim in doc.claims:
            row = table.add_row().cells
            row[0].text = f"{int(round(float(claim.get('confidence', 0)) * 100))}%"
            row[1].text = _plain(str(claim.get("text", "")))
    if doc.sources:
        out.add_heading("Sources", level=1)
        for src in doc.sources:
            out.add_paragraph(source_line(src), style="List Number")
    buf = io.BytesIO()
    out.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- PPTX
def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)] or [[]]


def _pptx(doc: Doc) -> bytes:  # noqa: C901 - flat renderer
    try:
        from pptx import Presentation
        from pptx.util import Pt
    except ImportError as exc:
        raise ExportUnavailable(f"PPTX export needs python-pptx: {_INSTALL}") from exc
    prs = Presentation()
    first = prs.slides.add_slide(prs.slide_layouts[0])
    first.shapes.title.text = doc.title
    first.placeholders[1].text = "\n".join(doc.meta)

    def bullets(title: str, lines: list[str], size: int = 16) -> None:
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = title
        frame = slide.placeholders[1].text_frame
        frame.word_wrap = True
        for i, line in enumerate(lines):
            para = frame.paragraphs[0] if i == 0 else frame.add_paragraph()
            para.text = line if len(line) <= 260 else line[:257].rstrip() + "..."
            para.font.size = Pt(size)

    sections: list[tuple[str, list[str]]] = []
    for block in doc.blocks:
        if block.kind.startswith("h") or not sections:
            sections.append((block.text if block.kind.startswith("h") else "Summary", []))
            if block.kind.startswith("h"):
                continue
        if block.kind == "table":
            sections[-1][1].extend(" | ".join(c for c in row if c) for row in block.rows[1:])
        else:
            sections[-1][1].append(block.text)
    for title, lines in sections:
        for n, part in enumerate(_chunks(lines, 3)):
            if part:
                bullets(title if n == 0 else f"{title} (cont.)", part, 14)
    if doc.claims:
        for n, part in enumerate(_chunks([claim_line(c) for c in doc.claims], 6)):
            bullets("Claim confidence" if n == 0 else "Claim confidence (cont.)", part, 13)
    if doc.sources:
        for n, part in enumerate(_chunks([source_line(s) for s in doc.sources], 7)):
            bullets("Sources" if n == 0 else "Sources (cont.)", part, 11)
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()
