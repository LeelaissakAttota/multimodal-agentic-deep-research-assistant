"""PDF text-extraction tool (offline, pypdf)."""

from __future__ import annotations

import io
import time
from typing import Any

from pydantic import Field
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from deep_research.domain.modality import Modality
from deep_research.evidence.evidence import Evidence
from deep_research.evidence.source import Source
from deep_research.tools.definition import ToolCapability, ToolDefinition, ToolInput, ToolOutput
from deep_research.tools.media import MediaDecodeError, decode_data_url
from deep_research.tools.tool import Tool, ToolRequest, ToolResult

PDF_TOOL_ID = "pdf_reader"
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 200
MAX_PDF_CHARS = 200_000
_PDF_MIMES = frozenset({"application/pdf", "application/x-pdf"})


class PdfReaderInput(ToolInput):
    pdf_data: str = Field(..., description="PDF as 'data:application/pdf;base64,<data>'.")
    source_title: str | None = None
    max_pages: int | None = None


class PdfReaderOutput(ToolOutput):
    text: str
    page_count: int
    pages_read: int
    truncated: bool
    metadata: dict[str, str] = Field(default_factory=dict)


def _fail(start: float, message: str, kind: str) -> ToolResult:
    return ToolResult(success=False, error=message, tool_name=PDF_TOOL_ID,
                      execution_time_ms=(time.monotonic() - start) * 1000,
                      metadata={"failure_kind": kind, "tool_identifier": PDF_TOOL_ID})


class PdfReaderTool(Tool):
    """Extract text from an embedded PDF with page/size/char limits."""

    def __init__(self, *, max_bytes: int = MAX_PDF_BYTES, max_pages: int = MAX_PDF_PAGES,
                 max_chars: int = MAX_PDF_CHARS) -> None:
        self.max_bytes = max_bytes
        self.max_pages = max_pages
        self.max_chars = max_chars

    @property
    def name(self) -> str:
        return PDF_TOOL_ID

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            identifier=PDF_TOOL_ID, name="PDF Reader", description="Extract text from a PDF document.",
            modality=Modality.PDF, capabilities=[ToolCapability.READ, ToolCapability.EXTRACT],
            input_schema=PdfReaderInput, output_schema=PdfReaderOutput, cost_class="FREE",
            network_required=False, auth_required=False, reliability="HIGH",
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, request: ToolRequest) -> ToolResult:
        start = time.monotonic()
        params = request.parameters
        try:
            raw, _ = decode_data_url(params.get("pdf_data", ""), allowed_mimes=_PDF_MIMES, max_bytes=self.max_bytes)
        except MediaDecodeError as exc:
            return _fail(start, str(exc), exc.kind)
        if not raw.startswith(b"%PDF-"):
            return _fail(start, "Payload is not a PDF file.", "invalid_pdf")
        requested = params.get("max_pages")
        page_limit = min(requested, self.max_pages) if isinstance(requested, int) and requested > 0 else self.max_pages
        try:
            text, page_count, pages_read, truncated, meta = self._extract(raw, page_limit)
        except _EncryptedPdf:
            return _fail(start, "PDF is encrypted; password-protected PDFs are not supported.", "encrypted_pdf")
        except (PdfReadError, ValueError, KeyError, TypeError, OSError):
            return _fail(start, "PDF could not be parsed.", "invalid_pdf")
        except Exception as exc:  # pypdf can raise many internal errors on hostile files
            return _fail(start, f"PDF parsing failed ({type(exc).__name__}).", "invalid_pdf")
        if not text.strip():
            return _fail(start, "PDF contains no extractable text (it may be scanned images).", "no_text")

        title = params.get("source_title") or meta.get("title") or None
        source = Source(title=title, description="Embedded PDF document",
                        metadata={"tool_identifier": PDF_TOOL_ID, "page_count": page_count})
        evidence = Evidence(source_id=source.id, content=text, content_type="pdf_text",
                            metadata={"tool_identifier": PDF_TOOL_ID, "pages_read": pages_read, "truncated": truncated})
        summary = text[:1_000]
        return ToolResult(
            success=True,
            output={
                "text": text, "page_count": page_count, "pages_read": pages_read, "truncated": truncated,
                "metadata": meta, "summary": summary, "evidence_ids": [evidence.id], "source_ids": [source.id],
                "evidence_objects": [evidence], "source_objects": [source],
            },
            tool_name=PDF_TOOL_ID, execution_time_ms=(time.monotonic() - start) * 1000,
            metadata={"tool_identifier": PDF_TOOL_ID, "page_count": page_count, "pages_read": pages_read},
        )

    def _extract(self, raw: bytes, page_limit: int) -> tuple[str, int, int, bool, dict[str, str]]:
        reader = PdfReader(io.BytesIO(raw), strict=False)
        if reader.is_encrypted:
            raise _EncryptedPdf()
        page_count = len(reader.pages)
        parts: list[str] = []
        total = 0
        pages_read = 0
        truncated = page_count > page_limit
        for page in reader.pages[:page_limit]:
            chunk = (page.extract_text() or "").strip()
            pages_read += 1
            if chunk:
                parts.append(chunk)
                total += len(chunk)
            if total >= self.max_chars:
                truncated = True
                break
        text = "\n\n".join(parts)[: self.max_chars]
        meta: dict[str, str] = {}
        info: Any = reader.metadata
        if info is not None:
            for key in ("title", "author", "subject"):
                value = getattr(info, key, None)
                if isinstance(value, str) and value.strip():
                    meta[key] = value.strip()[:500]
        return text, page_count, pages_read, truncated, meta


class _EncryptedPdf(Exception):
    pass
