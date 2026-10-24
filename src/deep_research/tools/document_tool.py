"""Office/text document reader (offline, stdlib only): DOCX, PPTX, TXT and Markdown.

DOCX and PPTX are zip packages of XML; text is read straight from
``word/document.xml`` (paragraphs, tables) and ``ppt/slides/slideN.xml``
(one *page* per slide, plus speaker notes). Output matches the PDF reader
(``pages`` + ``source_objects``) so attachment chunks are cited the same way.
"""

from __future__ import annotations

import io
import re
import time
import zipfile
from typing import Any
from xml.etree import ElementTree as ET

from pydantic import Field

from deep_research.domain.modality import Modality
from deep_research.evidence.evidence import Evidence
from deep_research.evidence.source import Source
from deep_research.tools.definition import ToolCapability, ToolDefinition, ToolInput, ToolOutput
from deep_research.tools.media import MediaDecodeError, decode_data_url
from deep_research.tools.tool import Tool, ToolRequest, ToolResult

DOCUMENT_TOOL_ID = "document_parser"
MAX_DOC_BYTES = 20 * 1024 * 1024
MAX_DOC_CHARS = 200_000
MAX_XML_BYTES = 30 * 1024 * 1024  # zip-bomb guard on the uncompressed XML we read
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
DOCUMENT_MIMES = frozenset({DOCX_MIME, PPTX_MIME, "text/plain", "text/markdown", "text/x-markdown",
                            "application/octet-stream", "application/zip"})
DOCUMENT_SUFFIXES = {".docx": DOCX_MIME, ".pptx": PPTX_MIME, ".txt": "text/plain", ".md": "text/markdown",
                     ".markdown": "text/markdown"}
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_PARA_CHARS = 1_500


class DocumentInput(ToolInput):
    document_data: str = Field(..., description="Document as a base64 data URL (docx, pptx, txt, md).")
    source_title: str | None = None


class DocumentOutput(ToolOutput):
    text: str
    page_count: int
    format: str


class DocumentError(ValueError):
    pass


def _fail(start: float, message: str, kind: str) -> ToolResult:
    return ToolResult(success=False, error=message, tool_name=DOCUMENT_TOOL_ID,
                      execution_time_ms=(time.monotonic() - start) * 1000,
                      metadata={"failure_kind": kind, "tool_identifier": DOCUMENT_TOOL_ID})


def _read_xml(zf: zipfile.ZipFile, name: str) -> ET.Element:
    info = zf.getinfo(name)
    if info.file_size > MAX_XML_BYTES:
        raise DocumentError(f"{name} is too large to read safely")
    data = zf.read(name)
    if b"<!DOCTYPE" in data[:2048] or b"<!ENTITY" in data[:4096]:
        raise DocumentError("document XML with DTD/entities is not accepted")
    return ET.fromstring(data)


def docx_pages(raw: bytes) -> list[dict[str, Any]]:
    """Paragraphs and table rows from a DOCX, grouped into ~1.5k-char sections."""

    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        root = _read_xml(zf, "word/document.xml")
    lines: list[str] = []
    body = root.find(f"{_W}body")
    for el in list(body) if body is not None else []:
        if el.tag == f"{_W}p":
            text = "".join(t.text or "" for t in el.iter(f"{_W}t")).strip()
            if text:
                lines.append(text)
        elif el.tag == f"{_W}tbl":
            for row in el.iter(f"{_W}tr"):
                cells = ["".join(t.text or "" for t in tc.iter(f"{_W}t")).strip() for tc in row.iter(f"{_W}tc")]
                if any(cells):
                    lines.append(" | ".join(cells))
    return _group(lines)


def pptx_pages(raw: bytes) -> list[dict[str, Any]]:
    """One page per slide (in slide order), speaker notes appended."""

    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = [n for n in zf.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
        names.sort(key=lambda n: int(re.findall(r"\d+", n)[-1]))
        pages: list[dict[str, Any]] = []
        for number, name in enumerate(names, 1):
            root = _read_xml(zf, name)
            paras = ["".join(t.text or "" for t in p.iter(f"{_A}t")).strip() for p in root.iter(f"{_A}p")]
            text = "\n".join(p for p in paras if p)
            notes_name = f"ppt/notesSlides/notesSlide{re.findall(r'[0-9]+', name)[-1]}.xml"
            if notes_name in zf.namelist():
                notes = [t.text or "" for t in _read_xml(zf, notes_name).iter(f"{_A}t")]
                note_text = " ".join(n.strip() for n in notes if n.strip() and not n.strip().isdigit())
                if note_text:
                    text += f"\nSpeaker notes: {note_text}"
            if text.strip():
                pages.append({"page": number, "text": text.strip()})
    return pages


def text_pages(raw: bytes) -> list[dict[str, Any]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    if "\x00" in text:
        raise DocumentError("file looks binary, not text")
    return _group([p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()])


def _group(lines: list[str]) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if current and size + len(line) > _PARA_CHARS:
            pages.append({"page": len(pages) + 1, "text": "\n".join(current)})
            current, size = [], 0
        current.append(line)
        size += len(line)
    if current:
        pages.append({"page": len(pages) + 1, "text": "\n".join(current)})
    return pages


def sniff_format(raw: bytes, mime: str) -> str:
    if raw[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                names = set(zf.namelist())
        except zipfile.BadZipFile as exc:
            raise DocumentError("corrupt zip package") from exc
        if "word/document.xml" in names:
            return "docx"
        if any(n.startswith("ppt/slides/") for n in names):
            return "pptx"
        raise DocumentError("zip file is not a DOCX or PPTX document")
    if mime in (DOCX_MIME, PPTX_MIME):
        raise DocumentError("file is not a valid Office package")
    return "md" if "markdown" in mime else "txt"


class DocumentReaderTool(Tool):
    """Read DOCX / PPTX / TXT / MD attachments into page-numbered text."""

    def __init__(self, *, max_bytes: int = MAX_DOC_BYTES, max_chars: int = MAX_DOC_CHARS) -> None:
        self.max_bytes = max_bytes
        self.max_chars = max_chars

    @property
    def name(self) -> str:
        return DOCUMENT_TOOL_ID

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            identifier=DOCUMENT_TOOL_ID, name="Document Reader", description="Read DOCX, PPTX, TXT and Markdown files.",
            modality=Modality.DOCUMENT, capabilities=[ToolCapability.READ, ToolCapability.EXTRACT],
            input_schema=DocumentInput, output_schema=DocumentOutput, cost_class="FREE",
            network_required=False, auth_required=False, reliability="HIGH",
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, request: ToolRequest) -> ToolResult:
        start = time.monotonic()
        params = request.parameters
        try:
            raw, mime = decode_data_url(params.get("document_data", ""), allowed_mimes=DOCUMENT_MIMES,
                                        max_bytes=self.max_bytes)
            fmt = sniff_format(raw, mime)
            pages = {"docx": docx_pages, "pptx": pptx_pages}.get(fmt, text_pages)(raw)
        except MediaDecodeError as exc:
            return _fail(start, str(exc), exc.kind)
        except (DocumentError, zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
            return _fail(start, f"Document could not be read ({exc}).", "invalid_document")
        total, kept = 0, []
        for page in pages:
            if total >= self.max_chars:
                break
            kept.append({"page": page["page"], "text": page["text"][: self.max_chars - total]})
            total += len(kept[-1]["text"])
        text = "\n\n".join(p["text"] for p in kept)
        if not text.strip():
            return _fail(start, "Document contains no readable text.", "empty_document")
        title = params.get("source_title") or f"{fmt.upper()} document"
        unit = "slides" if fmt == "pptx" else "sections"
        source = Source(title=title, description=f"Attached {fmt.upper()} document",
                        metadata={"tool_identifier": DOCUMENT_TOOL_ID, "format": fmt, "attachment": True})
        evidence = Evidence(source_id=source.id, content=text, content_type=f"{fmt}_text",
                            metadata={"tool_identifier": DOCUMENT_TOOL_ID, "format": fmt})
        return ToolResult(
            success=True,
            output={"text": text, "pages": kept, "page_count": len(pages), "pages_read": len(kept),
                    "truncated": len(kept) < len(pages) or total >= self.max_chars, "format": fmt, "unit": unit,
                    "summary": text[:1_000], "has_text": True, "evidence_ids": [evidence.id],
                    "source_ids": [source.id], "evidence_objects": [evidence], "source_objects": [source]},
            tool_name=DOCUMENT_TOOL_ID, execution_time_ms=(time.monotonic() - start) * 1000,
            metadata={"tool_identifier": DOCUMENT_TOOL_ID, "format": fmt, "page_count": len(pages)},
        )
