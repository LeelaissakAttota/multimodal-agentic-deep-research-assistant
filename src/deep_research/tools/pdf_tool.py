"""PDF reader tool (offline, pypdf) with image-only fallback.

* Text PDFs: per-page text (for chunked, citable synthesis) plus joined text.
* PDFs with no extractable text (scans) do **not** fail: the result carries a
  clear ``no extractable text`` note, page count/metadata and an inventory of
  embedded images (count, sizes). With a free vision gateway the first-page
  images are described; otherwise the note says what is present.
"""

from __future__ import annotations

import asyncio
import io
import time
from typing import Any

from pydantic import Field
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from deep_research.core.free_model_guard import ModelCapability
from deep_research.errors.base import ModelError
from deep_research.models.model_gateway import ModelGateway, ModelRequest

from deep_research.domain.modality import Modality
from deep_research.evidence.evidence import Evidence
from deep_research.evidence.source import Source
from deep_research.tools.definition import ToolCapability, ToolDefinition, ToolInput, ToolOutput
from deep_research.tools.media import MediaDecodeError, decode_data_url, to_data_url
from deep_research.tools.tool import Tool, ToolRequest, ToolResult

PDF_TOOL_ID = "pdf_reader"
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 200
MAX_PDF_CHARS = 200_000
_PDF_MIMES = frozenset({"application/pdf", "application/x-pdf"})
NO_TEXT_NOTE = "no extractable text"
MAX_INVENTORY_IMAGES = 50
MAX_DESCRIBED_IMAGES = 2
MAX_VISION_IMAGE_BYTES = 4 * 1024 * 1024
_VISION_PROMPT = ("This image was extracted from page {page} of a PDF with no text layer. Transcribe any visible "
                  "text and describe the content factually for a research report.")
_IMAGE_MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "JPG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}


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
                 max_chars: int = MAX_PDF_CHARS, vision_gateway: ModelGateway | None = None) -> None:
        self.vision_gateway = vision_gateway
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
            text, page_count, pages_read, truncated, meta, pages = self._extract(raw, page_limit)
        except _EncryptedPdf:
            return _fail(start, "PDF is encrypted; password-protected PDFs are not supported.", "encrypted_pdf")
        except (PdfReadError, ValueError, KeyError, TypeError, OSError):
            return _fail(start, "PDF could not be parsed.", "invalid_pdf")
        except Exception as exc:  # pypdf can raise many internal errors on hostile files
            return _fail(start, f"PDF parsing failed ({type(exc).__name__}).", "invalid_pdf")
        title = params.get("source_title") or meta.get("title") or None
        if not text.strip():
            return await self._image_only(start, raw, title, page_count, meta)

        source = Source(title=title, description="Embedded PDF document",
                        metadata={"tool_identifier": PDF_TOOL_ID, "page_count": page_count, "attachment": True})
        evidence = Evidence(source_id=source.id, content=text, content_type="pdf_text",
                            metadata={"tool_identifier": PDF_TOOL_ID, "pages_read": pages_read, "truncated": truncated})
        summary = text[:1_000]
        return ToolResult(
            success=True,
            output={
                "text": text, "pages": pages, "page_count": page_count, "pages_read": pages_read,
                "truncated": truncated, "metadata": meta, "summary": summary, "evidence_ids": [evidence.id],
                "source_ids": [source.id], "evidence_objects": [evidence], "source_objects": [source],
                "has_text": True,
            },
            tool_name=PDF_TOOL_ID, execution_time_ms=(time.monotonic() - start) * 1000,
            metadata={"tool_identifier": PDF_TOOL_ID, "page_count": page_count, "pages_read": pages_read,
                      "has_text": True},
        )

    async def _image_only(self, start: float, raw: bytes, title: str | None, page_count: int,
                          meta: dict[str, str]) -> ToolResult:
        """No text layer: report what is present and describe first-page images if a vision model exists."""

        images = _inventory_images(raw)
        ocr_pages = await asyncio.to_thread(_ocr_pages, images)
        descriptions, model_id, vision_error = await self._describe_first_page(images)
        lines = [f"PDF '{title or 'attachment'}': {NO_TEXT_NOTE} (likely scanned or image-only).",
                 f"Pages: {page_count}."]
        if meta:
            lines.append("Metadata: " + ", ".join(f"{k}={v}" for k, v in sorted(meta.items())) + ".")
        if images:
            sizes = ", ".join(f"p{img['page']}:{img['width']}x{img['height']}" for img in images[:10])
            lines.append(f"Embedded images: {len(images)} ({sizes}{', ...' if len(images) > 10 else ''}).")
        else:
            lines.append("Embedded images: none found (pages may be vector drawings).")
        for item in descriptions:
            lines.append(f"Image on page {item['page']} ({item['name']}): {item['description']}")
        if ocr_pages:
            lines.append(f"OCR ({ocr_pages[0]['engine']}) read text on {len(ocr_pages)} page(s).")
        if images and not descriptions:
            reason = vision_error or "no free vision model configured"
            lines.append(f"Image content not described ({reason}).")
        content = "\n".join(lines)
        source = Source(title=title, description="Embedded PDF document (no text layer)",
                        metadata={"tool_identifier": PDF_TOOL_ID, "page_count": page_count, "attachment": True})
        evidence = Evidence(source_id=source.id, content=content,
                            content_type="pdf_image_description" if descriptions else "pdf_metadata",
                            metadata={"tool_identifier": PDF_TOOL_ID, "model_id": model_id})
        public_images = [{k: v for k, v in img.items() if k != "data"} for img in images]
        return ToolResult(
            success=True,
            output={
                "text": "\n\n".join(p["text"] for p in ocr_pages),
                "pages": [{"page": p["page"], "text": p["text"]} for p in ocr_pages], "page_count": page_count,
                "ocr_engine": ocr_pages[0]["engine"] if ocr_pages else None, "pages_read": page_count, "truncated": False,
                "metadata": meta, "summary": content, "note": NO_TEXT_NOTE, "has_text": False,
                "images": public_images, "image_count": len(images), "image_descriptions": descriptions,
                "evidence_ids": [evidence.id], "source_ids": [source.id],
                "evidence_objects": [evidence], "source_objects": [source],
            },
            tool_name=PDF_TOOL_ID, execution_time_ms=(time.monotonic() - start) * 1000,
            metadata={"tool_identifier": PDF_TOOL_ID, "page_count": page_count, "has_text": False,
                      "note": NO_TEXT_NOTE, "image_count": len(images), "model_id": model_id,
                      "ocr_pages": len(ocr_pages),
                      "vision_error": vision_error},
        )

    async def _describe_first_page(self, images: list[dict[str, Any]]
                                   ) -> tuple[list[dict[str, Any]], str | None, str | None]:
        if self.vision_gateway is None or not images:
            return [], None, None
        first_page = images[0]["page"]
        targets = [img for img in images if img["page"] == first_page and img.get("data")][:MAX_DESCRIBED_IMAGES]
        out: list[dict[str, Any]] = []
        model_id: str | None = None
        error: str | None = None
        for img in targets:
            request = ModelRequest(
                prompt=_VISION_PROMPT.format(page=img["page"]),
                image_data=to_data_url(img["data"], img["mime"]),
                parameters={"required_capability": ModelCapability.VISION.value, "max_tokens": 700},
            )
            try:
                response = await self.vision_gateway.generate(request)
            except ModelError as exc:
                error = exc.error_code or "model_error"
                continue
            except Exception as exc:  # never let vision kill the run
                error = type(exc).__name__
                continue
            text = response.text.strip()
            if not text:
                error = "empty_response"
                continue
            model_id = response.model_id
            out.append({"page": img["page"], "name": img["name"], "description": text, "model_id": model_id})
        return out, model_id, None if out else error

    def _extract(self, raw: bytes, page_limit: int
                 ) -> tuple[str, int, int, bool, dict[str, str], list[dict[str, Any]]]:
        reader = PdfReader(io.BytesIO(raw), strict=False)
        if reader.is_encrypted:
            raise _EncryptedPdf()
        page_count = len(reader.pages)
        parts: list[str] = []
        pages: list[dict[str, Any]] = []
        total = 0
        pages_read = 0
        truncated = page_count > page_limit
        for number, page in enumerate(reader.pages[:page_limit], 1):
            chunk = (page.extract_text() or "").strip()
            pages_read += 1
            if chunk:
                parts.append(chunk)
                pages.append({"page": number, "text": chunk[: max(self.max_chars - total, 0)]})
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
        return text, page_count, pages_read, truncated, meta, pages


def _inventory_images(raw: bytes) -> list[dict[str, Any]]:
    """List embedded images (page, name, size); keep bytes only for vision-sized ones."""

    found: list[dict[str, Any]] = []
    try:
        reader = PdfReader(io.BytesIO(raw), strict=False)
        for number, page in enumerate(reader.pages, 1):
            for image in _page_images(page):
                found.append(_image_entry(number, image))
                if len(found) >= MAX_INVENTORY_IMAGES:
                    return found
    except Exception:  # inventory is best-effort on hostile/odd PDFs
        return found
    return found


def _page_images(page: Any) -> list[Any]:
    try:
        return list(page.images)
    except Exception:
        return []


def _image_entry(page_number: int, image: Any) -> dict[str, Any]:
    data: bytes = getattr(image, "data", b"") or b""
    width = height = 0
    fmt = ""
    pil = None
    try:
        pil = image.image
    except Exception:
        pil = None
    if pil is not None:
        width, height = int(pil.width), int(pil.height)
        fmt = str(pil.format or "").upper()
    mime = _IMAGE_MIME.get(fmt)
    if pil is not None and mime is None:
        # Re-encode unusual formats (e.g. raw/flate bitmaps) as PNG for the vision model.
        try:
            buf = io.BytesIO()
            pil.convert("RGB").save(buf, format="PNG")
            data, mime, fmt = buf.getvalue(), "image/png", "PNG"
        except Exception:
            mime = None
    usable = mime is not None and 0 < len(data) <= MAX_VISION_IMAGE_BYTES
    return {"page": page_number, "name": str(getattr(image, "name", "image")), "width": width, "height": height,
            "format": fmt or "unknown", "bytes": len(data), "mime": mime, "data": data if usable else None}


MAX_OCR_IMAGES = 20


def _ocr_pages(images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OCR embedded page images (scans) into per-page text; empty when no OCR engine is installed."""

    from deep_research.tools.ocr import ocr_image

    by_page: dict[int, list[str]] = {}
    engine = ""
    for img in [i for i in images if i.get("data")][:MAX_OCR_IMAGES]:
        result = ocr_image(img["data"])
        if result is not None:
            by_page.setdefault(int(img["page"]), []).append(result.text)
            engine = result.engine
    return [{"page": page, "text": "\n".join(texts), "engine": engine} for page, texts in sorted(by_page.items())]


class _EncryptedPdf(Exception):
    pass
