"""Speech-to-text: faster-whisper locally (default) or a free/local OpenAI-compatible API."""

from __future__ import annotations

import importlib.util
import threading
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from deep_research.media.config import MediaSettings
from deep_research.media.errors import MediaError, MediaUnavailable
from deep_research.media.timecode import Segment

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"})
_FREE_SUFFIX_HOSTS = frozenset({"openrouter.ai"})
_MODELS: dict[tuple[str, str, str], Any] = {}
_LOCK = threading.Lock()


class TranscriptionCancelled(MediaError):
    def __init__(self) -> None:
        super().__init__("Transcription cancelled after the timeout.", "transcription_timeout")


class Transcriber(Protocol):
    """``cancel`` (optional attribute) is set by the pipeline on timeout; transcribers stop at the next segment."""

    name: str

    def transcribe(self, path: Path) -> list[Segment]: ...


def whisper_available() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


def is_free_endpoint(api_base: str, model: str) -> bool:
    """Local servers, or ``:free`` models *on OpenRouter* (the suffix means nothing elsewhere)."""

    host = (urlparse(api_base).hostname or "").lower()
    if host in _LOCAL_HOSTS or host.endswith(".local"):
        return True
    return host in _FREE_SUFFIX_HOSTS and model.endswith(":free")


class LocalWhisper:
    """faster-whisper (CTranslate2). Decodes audio *and* video containers via PyAV, no ffmpeg needed."""

    def __init__(self, settings: MediaSettings) -> None:
        self.settings = settings
        self.name = f"faster-whisper:{settings.whisper_model}"
        self.cancel = threading.Event()

    def _model(self) -> Any:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise MediaUnavailable("faster-whisper for local transcription") from exc
        key = (self.settings.whisper_model, self.settings.whisper_device, self.settings.whisper_compute_type)
        with _LOCK:
            if key not in _MODELS:
                _MODELS[key] = WhisperModel(key[0], device=key[1], compute_type=key[2])
            return _MODELS[key]

    def transcribe(self, path: Path) -> list[Segment]:
        model = self._model()
        if self.cancel.is_set():
            raise TranscriptionCancelled()
        segments, _info = model.transcribe(str(path), language=self.settings.language or None, vad_filter=True)
        out: list[Segment] = []
        for s in segments:  # lazy generator: decoding happens per segment, so a timeout stops it here
            if self.cancel.is_set():
                raise TranscriptionCancelled()
            if str(s.text).strip():
                out.append(Segment(float(s.start), float(s.end), str(s.text).strip()))
        return out


class ApiTranscriber:
    """POST ``{api_base}/audio/transcriptions`` (verbose_json) - e.g. a local faster-whisper-server/speaches."""

    def __init__(self, settings: MediaSettings, client: httpx.Client | None = None) -> None:
        if not is_free_endpoint(settings.api_base, settings.api_model):
            raise MediaError("MDRA_MEDIA_API_BASE must be a local server or MDRA_MEDIA_API_MODEL a ':free' model "
                             "(paid transcription APIs are not used).", "paid_endpoint_refused")
        self.settings = settings
        self.client = client
        self.name = f"api:{settings.api_model}"
        self.cancel = threading.Event()

    def transcribe(self, path: Path) -> list[Segment]:
        key = self.settings.api_key.get_secret_value() if self.settings.api_key else ""
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        url = self.settings.api_base.rstrip("/") + "/audio/transcriptions"
        data = {"model": self.settings.api_model, "response_format": "verbose_json"}
        if self.settings.language:
            data["language"] = self.settings.language
        if self.cancel.is_set():
            raise TranscriptionCancelled()
        # Every connect/write/read phase is capped by the same timeout, so a stalled server cannot keep
        # the worker thread past roughly one extra timeout after the pipeline gave up.
        client = self.client or httpx.Client(timeout=httpx.Timeout(self.settings.timeout_seconds))
        try:
            with path.open("rb") as handle:
                response = client.post(url, headers=headers, data=data, files={"file": (path.name, handle)})
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            raise MediaError(f"Transcription API failed ({type(exc).__name__}).", "transcription_api_error") from exc
        except ValueError as exc:
            raise MediaError("Transcription API returned invalid JSON.", "transcription_api_error") from exc
        finally:
            if self.client is None:
                client.close()
        return parse_api_response(body)


def parse_api_response(body: Any) -> list[Segment]:
    if not isinstance(body, dict):
        raise MediaError("Transcription API returned an unexpected body.", "transcription_api_error")
    out: list[Segment] = []
    for seg in body.get("segments") or []:
        if isinstance(seg, dict) and str(seg.get("text") or "").strip():
            out.append(Segment(float(seg.get("start") or 0), float(seg.get("end") or 0), str(seg["text"]).strip()))
    if not out and str(body.get("text") or "").strip():
        out.append(Segment(0.0, float(body.get("duration") or 0), str(body["text"]).strip()))
    return out


def build_transcriber(settings: MediaSettings) -> Transcriber:
    if settings.transcriber == "api":
        return ApiTranscriber(settings)
    return LocalWhisper(settings)
