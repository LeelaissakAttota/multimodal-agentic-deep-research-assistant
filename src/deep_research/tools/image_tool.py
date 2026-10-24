"""Image analysis tool: Pillow validation/metadata + optional free vision description."""

from __future__ import annotations

import asyncio
import io
import time
import warnings
from typing import Any

from PIL import Image, UnidentifiedImageError
from pydantic import Field

from deep_research.core.free_model_guard import ModelCapability
from deep_research.domain.modality import Modality
from deep_research.errors.base import ModelError
from deep_research.evidence.evidence import Evidence
from deep_research.evidence.source import Source
from deep_research.models.model_gateway import ModelGateway, ModelRequest
from deep_research.tools.definition import ToolCapability, ToolDefinition, ToolInput, ToolOutput
from deep_research.tools.media import MediaDecodeError, decode_data_url, to_data_url
from deep_research.tools.ocr import ocr_image
from deep_research.tools.tool import Tool, ToolRequest, ToolResult

IMAGE_TOOL_ID = "image_analyzer"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"})
_FORMAT_FOR_MIME = {"image/png": "PNG", "image/jpeg": "JPEG", "image/jpg": "JPEG", "image/webp": "WEBP",
                    "image/gif": "GIF"}
_DEFAULT_PROMPT = "Describe this image factually and in detail for a research report. Transcribe any visible text."


class ImageAnalysisInput(ToolInput):
    image_data: str = Field(..., description="Image as 'data:image/<png|jpeg|webp|gif>;base64,<data>'.")
    prompt: str = _DEFAULT_PROMPT
    source_title: str | None = None


class ImageAnalysisOutput(ToolOutput):
    width: int
    height: int
    format: str
    mode: str
    description: str | None = None
    model_id: str | None = None


def _fail(start: float, message: str, kind: str, **extra: Any) -> ToolResult:
    return ToolResult(success=False, error=message, tool_name=IMAGE_TOOL_ID,
                      execution_time_ms=(time.monotonic() - start) * 1000,
                      metadata={"failure_kind": kind, "tool_identifier": IMAGE_TOOL_ID, **extra})


class ImageAnalysisTool(Tool):
    """Validate an image, extract metadata, optionally describe it via a free vision model.

    With no gateway (no OpenRouter key) the tool still succeeds with
    metadata only (``description_source="metadata_only"``); it never
    invents a description.
    """

    def __init__(self, gateway: ModelGateway | None = None, *, use_settings: bool = True,
                 max_bytes: int = MAX_IMAGE_BYTES, max_pixels: int = MAX_IMAGE_PIXELS) -> None:
        if gateway is None and use_settings:
            from deep_research.core.config import settings
            from deep_research.core.provider_wiring import build_provider_gateway
            gateway = build_provider_gateway(settings, capability=ModelCapability.VISION)
        self._gateway = gateway
        self.max_bytes = max_bytes
        self.max_pixels = max_pixels

    @property
    def name(self) -> str:
        return IMAGE_TOOL_ID

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            identifier=IMAGE_TOOL_ID, name="Image Analyzer",
            description="Validate an image, extract metadata, and describe it with a free vision model.",
            modality=Modality.IMAGE, capabilities=[ToolCapability.ANALYZE, ToolCapability.DESCRIBE],
            input_schema=ImageAnalysisInput, output_schema=ImageAnalysisOutput, cost_class="FREE",
            network_required=False, auth_required=False, reliability="MEDIUM",
            metadata={"optional_model_capability": ModelCapability.VISION.value},
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, request: ToolRequest) -> ToolResult:
        start = time.monotonic()
        params = request.parameters
        image_data = params.get("image_data", "")
        try:
            raw, mime = decode_data_url(image_data, allowed_mimes=_IMAGE_MIMES, max_bytes=self.max_bytes)
        except MediaDecodeError as exc:
            return _fail(start, str(exc), exc.kind)
        try:
            info = self._inspect(raw)
        except _TooManyPixels:
            return _fail(start, "Image dimensions exceed the pixel limit.", "too_large")
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
            return _fail(start, "Payload is not a valid image.", "invalid_image")
        if _FORMAT_FOR_MIME.get(mime) != info["format"]:
            return _fail(start, f"Declared type {mime} does not match actual format {info['format']}.",
                         "type_mismatch")

        prompt = params.get("prompt") if isinstance(params.get("prompt"), str) else _DEFAULT_PROMPT
        description, model_id, vision_error = await self._describe(to_data_url(raw, mime), prompt or _DEFAULT_PROMPT)
        ocr = await asyncio.to_thread(ocr_image, raw)
        title = params.get("source_title") or None
        source = Source(title=title, description="Embedded image",
                        metadata={"tool_identifier": IMAGE_TOOL_ID, **info})
        content = description or (
            f"Image ({info['format']}, {info['width']}x{info['height']}, mode {info['mode']}); no description available."
        )
        if ocr is not None:
            content = f"{content}\n\nText found in the image (OCR, {ocr.engine}):\n{ocr.text}"
        evidence = Evidence(source_id=source.id, content=content,
                            content_type="image_description" if description else "image_metadata",
                            metadata={"tool_identifier": IMAGE_TOOL_ID, "model_id": model_id})
        return ToolResult(
            success=True,
            output={**info, "description": description, "model_id": model_id, "summary": content,
                    "ocr_text": ocr.text if ocr else None, "ocr_engine": ocr.engine if ocr else None,
                    "evidence_ids": [evidence.id], "source_ids": [source.id],
                    "evidence_objects": [evidence], "source_objects": [source]},
            tool_name=IMAGE_TOOL_ID, execution_time_ms=(time.monotonic() - start) * 1000,
            metadata={"tool_identifier": IMAGE_TOOL_ID, "model_id": model_id, "vision_error": vision_error,
                      "description_source": "vision_model" if description else "ocr" if ocr else "metadata_only",
                      "ocr_engine": ocr.engine if ocr else None},
        )

    def _inspect(self, raw: bytes) -> dict[str, Any]:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            try:
                with Image.open(io.BytesIO(raw)) as probe:
                    width, height = probe.size
                    if width * height > self.max_pixels:
                        raise _TooManyPixels()
                    probe.verify()
                with Image.open(io.BytesIO(raw)) as img:
                    img.load()
                    return {"width": img.width, "height": img.height, "format": str(img.format),
                            "mode": img.mode, "frames": int(getattr(img, "n_frames", 1)), "bytes": len(raw)}
            except (Image.DecompressionBombWarning, Image.DecompressionBombError):
                raise _TooManyPixels()

    async def _describe(self, data_url: str, prompt: str) -> tuple[str | None, str | None, str | None]:
        if self._gateway is None:
            return None, None, None
        request = ModelRequest(prompt=prompt, image_data=data_url,
                               parameters={"required_capability": ModelCapability.VISION.value, "max_tokens": 600})
        try:
            response = await self._gateway.generate(request)
        except ModelError as exc:
            return None, None, exc.error_code or "model_error"
        except Exception as exc:
            return None, None, type(exc).__name__
        text = response.text.strip()
        if not text:
            return None, None, "empty_response"
        return text, response.model_id, None


class _TooManyPixels(Exception):
    pass
