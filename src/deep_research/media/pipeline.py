"""Run one media item end to end: transcript + keyframe captions -> timestamped segments."""

from __future__ import annotations

import asyncio
import base64
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from deep_research.media import frames as frames_mod
from deep_research.media import remote
from deep_research.media.config import MediaSettings
from deep_research.media.errors import MediaError, MediaUnavailable
from deep_research.media.timecode import Segment, fmt_ts
from deep_research.media.transcribe import Transcriber, build_transcriber, whisper_available

FRAME_PROMPT = ("Describe this video frame for a research report in 2-3 factual sentences: the scene, any people's "
                "actions, and all readable on-screen text, charts or slide titles. Do not guess beyond what is visible.")
_SUFFIX = {"audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav",
           "audio/wave": ".wav", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/ogg": ".ogg",
           "audio/webm": ".webm", "audio/flac": ".flac", "audio/x-flac": ".flac", "video/mp4": ".mp4",
           "video/webm": ".webm", "video/quicktime": ".mov", "video/x-matroska": ".mkv", "video/ogg": ".ogv"}
#: Transcription runs on its own small pool: a timed-out job that is still finishing its current segment
#: (or a model load, which cannot be interrupted) occupies at most these threads; new jobs queue behind it.
TRANSCRIBE_WORKERS = 2
_TRANSCRIBE_POOL = ThreadPoolExecutor(max_workers=TRANSCRIBE_WORKERS, thread_name_prefix="dr-transcribe")
AUDIO_MIMES = frozenset(m for m in _SUFFIX if m.startswith("audio/"))
VIDEO_MIMES = frozenset(m for m in _SUFFIX if m.startswith("video/"))


@dataclass
class MediaAnalysis:
    media_kind: str  # "audio" | "video"
    segments: list[Segment] = field(default_factory=list)
    duration: float = 0.0
    transcript_source: str = ""  # "captions (manual)" | "captions (auto)" | "faster-whisper:base" | "api:..."
    frame_model: str | None = None
    frames_captioned: int = 0
    title: str | None = None
    notes: list[str] = field(default_factory=list)


async def caption_frame(gateway: Any, jpeg: bytes) -> tuple[str | None, str | None]:
    from deep_research.core.free_model_guard import ModelCapability
    from deep_research.models.model_gateway import ModelRequest

    data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
    request = ModelRequest(prompt=FRAME_PROMPT, image_data=data_url,
                           parameters={"required_capability": ModelCapability.VISION.value, "max_tokens": 300})
    try:
        response = await gateway.generate(request)
    except Exception:  # noqa: BLE001 - a failed caption is reported as a note, never invented
        return None, None
    text = str(getattr(response, "text", "") or "").strip()
    return (text or None), getattr(response, "model_id", None)


class MediaPipeline:
    def __init__(self, settings: MediaSettings, *, vision_gateway: Any = None,
                 transcriber_factory: Callable[[MediaSettings], Transcriber] = build_transcriber,
                 url_validator: Callable[[str], bool] | None = None) -> None:
        self.settings = settings
        self.url_validator = url_validator
        self.vision_gateway = vision_gateway
        self.transcriber_factory = transcriber_factory

    async def analyze_bytes(self, raw: bytes, mime: str, title: str | None = None) -> MediaAnalysis:
        kind = "video" if mime in VIDEO_MIMES else "audio"
        with tempfile.TemporaryDirectory(prefix="dr-media-") as tmp:
            path = Path(tmp) / f"input{_SUFFIX.get(mime, '.bin')}"
            path.write_bytes(raw)
            result = MediaAnalysis(media_kind=kind, title=title)
            await self._local_file(path, Path(tmp), result, frames=kind == "video")
            return result

    async def analyze_url(self, url: str, title: str | None = None) -> MediaAnalysis:
        info = await asyncio.to_thread(remote.fetch_info, url, self.url_validator)
        duration = float(info.get("duration") or 0)
        if duration > self.settings.max_duration_seconds:
            raise MediaError(f"Media is {fmt_ts(duration)} long; the limit is "
                             f"{fmt_ts(self.settings.max_duration_seconds)} (MDRA_MEDIA_MAX_DURATION_SECONDS).",
                             "media_too_long")
        has_video = bool(info.get("vcodec") not in (None, "none") or info.get("width"))
        result = MediaAnalysis(media_kind="video" if has_video else "audio", duration=duration,
                               title=title or info.get("title"))
        if self.settings.prefer_captions:
            picked = remote.pick_caption(info, self.settings.language)
            if picked:
                cap_url, ext, auto = picked
                try:
                    remote.check_url(cap_url, self.url_validator)  # yt-dlp info is untrusted input
                    raw = await asyncio.to_thread(remote.download_caption, cap_url, self.url_validator)
                    result.segments = remote.parse_captions(raw, ext)
                    result.transcript_source = f"captions ({'auto' if auto else 'manual'})"
                except MediaError as exc:
                    result.notes.append(f"captions unusable ({exc}); transcribing instead")
        want_frames = result.media_kind == "video" and self.settings.keyframes > 0 and self.settings.youtube_frames
        if result.segments and not want_frames:
            if result.media_kind == "video" and self.settings.keyframes > 0:
                result.notes.append("keyframes skipped for remote video (set MDRA_MEDIA_YOUTUBE_FRAMES=true)")
            return result
        with tempfile.TemporaryDirectory(prefix="dr-media-") as tmp:
            path = await asyncio.to_thread(remote.download, url, Path(tmp), self.settings, video=want_frames,
                                           validator=self.url_validator)
            await self._local_file(path, Path(tmp), result, frames=want_frames,
                                   transcribe=not result.segments)
        return result

    async def _local_file(self, path: Path, tmp: Path, result: MediaAnalysis, *, frames: bool,
                          transcribe: bool = True) -> None:
        backend = frames_mod.frame_backend()
        if backend is not None:
            try:
                meta = await asyncio.to_thread(frames_mod.probe, path)
                result.duration = result.duration or float(meta["duration"])
                if frames and not meta["has_video"]:
                    result.media_kind, frames = "audio", False
            except MediaError as exc:
                result.notes.append(str(exc))
        if result.duration > self.settings.max_duration_seconds:
            raise MediaError(f"Media is {fmt_ts(result.duration)} long; the limit is "
                             f"{fmt_ts(self.settings.max_duration_seconds)}.", "media_too_long")
        if transcribe:
            await self._transcribe(path, tmp, result)
        if frames:
            await self._frames(path, result, backend)

    async def _transcribe(self, path: Path, tmp: Path, result: MediaAnalysis) -> None:
        if self.settings.transcriber == "local" and not whisper_available():
            raise MediaUnavailable("faster-whisper for local transcription")
        transcriber = self.transcriber_factory(self.settings)
        source = path
        if self.settings.transcriber == "api":
            source = await asyncio.to_thread(frames_mod.extract_audio, path, tmp)
        cancel = getattr(transcriber, "cancel", None)
        loop = asyncio.get_running_loop()
        try:
            segments = await asyncio.wait_for(loop.run_in_executor(_TRANSCRIBE_POOL, transcriber.transcribe, source),
                                              timeout=self.settings.timeout_seconds)
        except TimeoutError as exc:
            if isinstance(cancel, threading.Event):
                cancel.set()  # the worker stops at its next segment / before its next request
            raise MediaError(f"Transcription timed out after {self.settings.timeout_seconds:.0f}s.",
                             "transcription_timeout") from exc
        except MediaError:
            raise
        except Exception as exc:  # decoder / model runtime errors: report, never fake a transcript
            raise MediaError(f"Transcription failed ({type(exc).__name__}: {str(exc)[:200]}).",
                             "transcription_failed") from exc
        result.segments = list(result.segments) + segments
        result.transcript_source = transcriber.name
        if not segments:
            result.notes.append("no speech detected")

    async def _frames(self, path: Path, result: MediaAnalysis, backend: str | None) -> None:
        if backend is None:
            result.notes.append('keyframes skipped: install ffmpeg or PyAV (pip install -e ".[media]")')
            return
        if self.vision_gateway is None:
            result.notes.append("keyframes not captioned: no free vision model configured")
            return
        times = frames_mod.sample_times(result.duration, self.settings.keyframes)
        shots = await asyncio.to_thread(frames_mod.extract_frames, path, times)
        captioned: list[Segment] = []
        for t, jpeg in shots:
            text, model = await caption_frame(self.vision_gateway, jpeg)
            if text:
                captioned.append(Segment(t, t, text, kind="frame"))
                result.frame_model = result.frame_model or model
        if shots and not captioned:
            result.notes.append("vision model returned no frame captions")
        result.frames_captioned = len(captioned)
        result.segments = list(result.segments) + captioned
