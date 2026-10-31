"""Audio/video/YouTube attachment analyser (optional ``media`` extra; off by default).

Transcribes speech (YouTube captions first, else faster-whisper locally or a
free/local OpenAI-compatible API), captions sampled keyframes with the
configured free vision model, and returns ``pages`` keyed by ``mm:ss`` so
citations point at the moment in the recording.
"""

from __future__ import annotations

import time
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from pydantic import Field

from deep_research.domain.modality import Modality
from deep_research.evidence.evidence import Evidence
from deep_research.evidence.source import Source
from deep_research.media.config import MediaSettings
from deep_research.media.errors import MediaError
from deep_research.media.pipeline import AUDIO_MIMES, VIDEO_MIMES, MediaAnalysis, MediaPipeline
from deep_research.media.timecode import fmt_ts, windows
from deep_research.tools.definition import ToolCapability, ToolDefinition, ToolInput, ToolOutput
from deep_research.tools.media import MediaDecodeError, decode_data_url
from deep_research.tools.tool import Tool, ToolRequest, ToolResult

MEDIA_TOOL_ID = "media_analyzer"
MAX_MEDIA_BYTES = 20 * 1024 * 1024
MAX_MEDIA_CHARS = 200_000
MEDIA_MIMES = AUDIO_MIMES | VIDEO_MIMES | frozenset({"application/octet-stream"})
DISABLED_MESSAGE = ("Audio/video analysis is off. Set MDRA_MEDIA_ENABLED=true and install the extra: "
                    'pip install -e ".[media]".')


class MediaInput(ToolInput):
    media_data: str = Field(..., description="Audio/video as a base64 data URL, or a YouTube/media http(s) URL.")
    source_title: str | None = None


class MediaOutput(ToolOutput):
    text: str
    media_kind: str
    duration_seconds: float


def _fail(start: float, message: str, kind: str) -> ToolResult:
    return ToolResult(success=False, error=message, tool_name=MEDIA_TOOL_ID,
                      execution_time_ms=(time.monotonic() - start) * 1000,
                      metadata={"failure_kind": kind, "tool_identifier": MEDIA_TOOL_ID})


def _default_validator(url: str) -> bool:
    from deep_research.tools.web_tools import is_public_http_url

    return is_public_http_url(url)


def at_time(url: str, seconds: float) -> str:
    """The same URL with ``t=<seconds>`` (YouTube deep link); other hosts get it too, harmlessly."""

    parsed = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parsed.query) if k != "t"] + [("t", f"{int(seconds)}s")]
    return urlunparse(parsed._replace(query=urlencode(query)))


class MediaAnalysisTool(Tool):
    """Transcribe + keyframe-caption an audio/video file or YouTube link into timestamped pages."""

    def __init__(self, *, settings: MediaSettings | None = None, vision_gateway: Any = None,
                 pipeline: MediaPipeline | None = None, max_bytes: int = MAX_MEDIA_BYTES,
                 url_validator: Callable[[str], bool] = _default_validator) -> None:
        self.settings = settings or MediaSettings()
        self.pipeline = pipeline or MediaPipeline(self.settings, vision_gateway=vision_gateway,
                                                  url_validator=url_validator)
        self.max_bytes = max_bytes
        self.url_validator = url_validator

    @property
    def name(self) -> str:
        return MEDIA_TOOL_ID

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            identifier=MEDIA_TOOL_ID, name="Audio & Video Analysis",
            description="Transcribe audio/video/YouTube with timestamps and caption video keyframes.",
            modality=Modality.VIDEO, capabilities=[ToolCapability.READ, ToolCapability.EXTRACT],
            input_schema=MediaInput, output_schema=MediaOutput, cost_class="FREE",
            network_required=False, auth_required=False, reliability="MEDIUM",
        )

    async def health_check(self) -> bool:
        return self.settings.enabled

    async def execute(self, request: ToolRequest) -> ToolResult:
        start = time.monotonic()
        if not self.settings.enabled:
            return _fail(start, DISABLED_MESSAGE, "media_disabled")
        params = request.parameters
        data = str(params.get("media_data") or "").strip()
        title = params.get("source_title")
        url: str | None = None
        try:
            if data.lower().startswith(("http://", "https://")):
                if not self.url_validator(data):
                    return _fail(start, "Only public http(s) media links are allowed.", "blocked_url")
                url = data
                analysis = await self.pipeline.analyze_url(data, title)
            else:
                raw, mime = decode_data_url(data, allowed_mimes=MEDIA_MIMES, max_bytes=self.max_bytes)
                if mime == "application/octet-stream":
                    mime = _sniff(raw)
                analysis = await self.pipeline.analyze_bytes(raw, mime, title)
        except MediaDecodeError as exc:
            return _fail(start, str(exc), exc.kind)
        except MediaError as exc:
            return _fail(start, str(exc), exc.kind)
        return self._result(start, analysis, url, title)

    def _result(self, start: float, analysis: MediaAnalysis, url: str | None, title: Any) -> ToolResult:
        pages = windows(analysis.segments, self.settings.window_seconds)
        total, kept = 0, []
        for page in pages:
            if total >= MAX_MEDIA_CHARS:
                break
            page = dict(page, text=str(page["text"])[: MAX_MEDIA_CHARS - total])
            if url:
                page["url"] = at_time(url, float(page["start"]))
            kept.append(page)
            total += len(page["text"])
        if not kept:
            note = "; ".join(analysis.notes) or "no speech or captioned frames"
            return _fail(start, f"Nothing usable in this {analysis.media_kind} ({note}).", "empty_media")
        text = "\n\n".join(p["text"] for p in kept)
        kind = analysis.media_kind
        name = str(title or analysis.title or (url or f"{kind} attachment"))
        source = Source(title=name, url=url, description=f"Attached {kind} ({fmt_ts(analysis.duration)})",  # type: ignore[arg-type]
                        metadata={"tool_identifier": MEDIA_TOOL_ID, "attachment": True, "media_kind": kind,
                                  "transcript_source": analysis.transcript_source})
        evidence = Evidence(source_id=source.id, content=text, content_type=f"{kind}_transcript",
                            metadata={"tool_identifier": MEDIA_TOOL_ID, "media_kind": kind})
        frames = sum(1 for p in kept if p.get("kind") == "frame")
        return ToolResult(
            success=True,
            output={"text": text, "pages": kept, "page_count": len(pages), "pages_read": len(kept),
                    "truncated": len(kept) < len(pages), "media_kind": kind, "attachment_kind": kind,
                    "timestamps": True, "duration_seconds": analysis.duration, "duration": fmt_ts(analysis.duration),
                    "transcript_source": analysis.transcript_source, "frame_model": analysis.frame_model,
                    "frames_captioned": frames, "notes": analysis.notes, "note": "; ".join(analysis.notes) or None,
                    "summary": text[:1_000], "has_text": True, "evidence_ids": [evidence.id],
                    "source_ids": [source.id], "evidence_objects": [evidence], "source_objects": [source]},
            tool_name=MEDIA_TOOL_ID, execution_time_ms=(time.monotonic() - start) * 1000,
            metadata={"tool_identifier": MEDIA_TOOL_ID, "media_kind": kind,
                      "transcript_source": analysis.transcript_source, "frames_captioned": frames},
        )


def _sniff(raw: bytes) -> str:
    head = raw[:16]
    if head[4:8] == b"ftyp":
        return "video/mp4"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm"
    if head.startswith(b"RIFF") and raw[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith((b"ID3", b"\xff\xfb", b"\xff\xf3")):
        return "audio/mpeg"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"fLaC"):
        return "audio/flac"
    return "application/octet-stream"
