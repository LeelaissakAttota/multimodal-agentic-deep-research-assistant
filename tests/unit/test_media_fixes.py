"""Fixes after the phase 7 (B) edge-case review: SSRF on yt-dlp URLs, ':free' host rule,
malformed json3, and transcription timeouts. yt-dlp, whisper and the network are faked."""

from __future__ import annotations

import asyncio
import base64
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any

import httpx
import pytest

from deep_research.media import frames as frames_mod
from deep_research.media import pipeline as pipeline_mod
from deep_research.media import remote, transcribe
from deep_research.media.config import MediaSettings
from deep_research.media.errors import MediaError
from deep_research.media.pipeline import MediaPipeline
from deep_research.media.timecode import Segment
from deep_research.media.transcribe import (ApiTranscriber, LocalWhisper, TranscriptionCancelled,
                                            is_free_endpoint)
from deep_research.tools.media_tool import MediaAnalysisTool
from deep_research.tools.tool import ToolRequest

AUDIO = "data:audio/mpeg;base64," + base64.b64encode(b"ID3fake").decode()


def public(url: str) -> bool:
    return "internal" not in url and "169.254" not in url


# ----- fake yt-dlp -------------------------------------------------------------------------------

class _Resp:
    def __init__(self, url: str, body: bytes = b"ok") -> None:
        self.url, self.body, self.closed = url, body, False

    def read(self) -> bytes:
        return self.body

    def close(self) -> None:
        self.closed = True


def fake_ytdlp(monkeypatch: pytest.MonkeyPatch, *, redirects: dict[str, str] | None = None,
               formats: list[str] | None = None) -> dict[str, list[Any]]:
    log: dict[str, list[Any]] = {"sent": [], "downloaded": [], "responses": []}

    class YoutubeDL:
        def __init__(self, params: dict[str, Any]) -> None:
            self.params = params

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *a: Any) -> None:
            return None

        def urlopen(self, req: Any) -> _Resp:
            url = req if isinstance(req, str) else req.url
            log["sent"].append(url)
            resp = _Resp((redirects or {}).get(url, url))
            log["responses"].append(resp)
            return resp

        def extract_info(self, url: str, download: bool = False) -> dict[str, Any]:
            self.urlopen(url)  # the extractor's page request goes through the guarded urlopen
            fmts = formats or ["https://cdn.example/a.m4a"]
            return {"requested_formats": [{"url": u} for u in fmts], "ext": "m4a"}

        def process_ie_result(self, info: dict[str, Any], download: bool = True) -> dict[str, Any]:
            for f in info["requested_formats"]:
                self.urlopen(f["url"])
                log["downloaded"].append(f["url"])
            return info

        def prepare_filename(self, info: dict[str, Any]) -> str:
            path = Path(self.params["outtmpl"].replace("%(ext)s", "m4a"))
            path.write_bytes(b"x")
            return str(path)

    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = YoutubeDL  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yt_dlp", module)
    return log


def test_download_checks_every_chosen_format_url_before_fetching(monkeypatch: pytest.MonkeyPatch,
                                                                 tmp_path: Path) -> None:
    log = fake_ytdlp(monkeypatch, formats=["https://cdn.example/v.mp4", "http://169.254.169.254/a"])
    with pytest.raises(MediaError) as err:
        remote.download("https://www.youtube.com/watch?v=x", tmp_path, MediaSettings(), video=True, validator=public)
    assert err.value.kind == "blocked_url" and log["downloaded"] == []


def test_download_happy_path_and_native_downloader(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    log = fake_ytdlp(monkeypatch)
    path = remote.download("https://www.youtube.com/watch?v=x", tmp_path, MediaSettings(), video=False,
                           validator=public)
    assert path.exists() and log["downloaded"] == ["https://cdn.example/a.m4a"]
    assert remote._ydl({}, public).params["hls_prefer_native"] is True


def test_redirect_to_private_host_is_blocked_and_body_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    log = fake_ytdlp(monkeypatch, redirects={"https://captions.example/en.vtt": "http://internal.svc/secret"})
    with pytest.raises(MediaError) as err:
        remote.download_caption("https://captions.example/en.vtt", public)
    assert err.value.kind == "blocked_url" and log["responses"][0].closed


def test_private_caption_and_initial_urls_never_sent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    log = fake_ytdlp(monkeypatch)
    with pytest.raises(MediaError):
        remote.download_caption("http://169.254.169.254/latest", public)
    with pytest.raises(MediaError):
        remote.download("http://internal.svc/v.mp4", tmp_path, MediaSettings(), video=False, validator=public)
    assert log["sent"] == []


def test_default_validator_is_the_public_address_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr("deep_research.tools.web_tools.is_public_http_url", lambda u: seen.append(u) or False)
    with pytest.raises(MediaError):
        remote.check_url("https://example.org/x")
    assert seen == ["https://example.org/x"]


def test_tool_validator_reaches_caption_check(monkeypatch: pytest.MonkeyPatch) -> None:
    info = {"duration": 10, "vcodec": "none", "subtitles": {"en": [{"ext": "vtt", "url": "http://internal.svc/c"}]}}
    monkeypatch.setattr(remote, "fetch_info", lambda url, validator=None: info)
    monkeypatch.setattr(remote, "download_caption", lambda *a, **k: pytest.fail("fetched private caption"))
    monkeypatch.setattr(remote, "download", lambda *a, **k: (_ for _ in ()).throw(MediaError("no dl", "x")))
    result = asyncio.run(MediaAnalysisTool(settings=MediaSettings(enabled=True), url_validator=public).execute(
        ToolRequest(tool_name="media_analyzer", parameters={"media_data": "https://www.youtube.com/watch?v=x"})))
    assert not result.success


# ----- ':free' only on OpenRouter -------------------------------------------------------------------

@pytest.mark.parametrize(("base", "model", "ok"), [
    ("https://openrouter.ai/api/v1", "openai/whisper:free", True),
    ("https://openrouter.ai/api/v1", "openai/whisper", False),
    ("https://api.openai.com/v1", "whisper-1:free", False),
    ("https://evil.example/v1", "x:free", False),
    ("https://openrouter.ai.evil.example/v1", "x:free", False),
    ("http://localhost:8000/v1", "whisper-1", True),
    ("http://127.0.0.1:8000/v1", "anything", True),
    ("https://gpu.local/v1", "whisper-1", True),
])
def test_free_suffix_only_counts_on_openrouter(base: str, model: str, ok: bool) -> None:
    assert is_free_endpoint(base, model) is ok


# ----- malformed json3 ----------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [b"[]", b'"text"', b"42", b"null", b'{"events": {}}', b'{"events": [1]}',
                                 b'{"events": [{"segs": "x"}]}',
                                 b'{"events": [{"tStartMs": "abc", "segs": [{"utf8": "hi"}]}]}'])
def test_malformed_json3_is_media_error(raw: bytes) -> None:
    with pytest.raises(MediaError) as err:
        remote.parse_captions(raw, "json3")
    assert err.value.kind == "invalid_captions"


def test_bad_captions_fall_back_to_transcription(monkeypatch: pytest.MonkeyPatch) -> None:
    info = {"duration": 10, "vcodec": "none", "subtitles": {"en": [{"ext": "json3", "url": "https://c.example/j"}]}}
    monkeypatch.setattr(remote, "fetch_info", lambda url, validator=None: info)
    monkeypatch.setattr(remote, "download_caption", lambda url, validator=None: b"[]")
    monkeypatch.setattr(pipeline_mod, "whisper_available", lambda: True)
    monkeypatch.setattr(frames_mod, "frame_backend", lambda: None)

    def fake_download(url: str, out_dir: Path, settings: MediaSettings, *, video: bool, validator: Any = None) -> Path:
        path = out_dir / "audio.m4a"
        path.write_bytes(b"x")
        return path

    monkeypatch.setattr(remote, "download", fake_download)

    class Fake:
        name = "fake"

        def transcribe(self, path: Path) -> list[Segment]:
            return [Segment(0, 2, "spoken words")]

    result = asyncio.run(MediaPipeline(MediaSettings(enabled=True), transcriber_factory=lambda s: Fake(),
                                       url_validator=lambda u: True).analyze_url("https://youtu.be/x"))
    assert result.transcript_source == "fake" and any("captions unusable" in n for n in result.notes)


# ----- timeouts stop the work -----------------------------------------------------------------------

class _SlowModel:
    def __init__(self) -> None:
        self.yielded = 0

    def transcribe(self, path: str, **kw: Any) -> tuple[Any, None]:
        def gen() -> Any:
            for i in range(10_000):
                time.sleep(0.02)
                self.yielded += 1
                yield types.SimpleNamespace(start=i, end=i + 1, text=f"w{i}")
        return gen(), None


def test_whisper_stops_at_next_segment_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = MediaSettings(enabled=True, timeout_seconds=0.2)
    model = _SlowModel()
    monkeypatch.setitem(transcribe._MODELS, (settings.whisper_model, settings.whisper_device,
                                             settings.whisper_compute_type), model)
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=None))
    monkeypatch.setattr(pipeline_mod, "whisper_available", lambda: True)
    monkeypatch.setattr(frames_mod, "frame_backend", lambda: None)
    made: list[LocalWhisper] = []

    def factory(s: MediaSettings) -> LocalWhisper:
        made.append(LocalWhisper(s))
        return made[-1]

    result = asyncio.run(MediaAnalysisTool(settings=settings, pipeline=MediaPipeline(
        settings, transcriber_factory=factory)).execute(ToolRequest(tool_name="media_analyzer",
                                                                    parameters={"media_data": AUDIO})))
    assert result.metadata["failure_kind"] == "transcription_timeout" and made[0].cancel.is_set()
    time.sleep(0.15)
    stopped_at = model.yielded
    time.sleep(0.2)
    assert model.yielded == stopped_at < 100  # the worker thread is no longer decoding


def test_api_transcriber_honours_cancel_and_uses_bounded_timeout(tmp_path: Path) -> None:
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"ID3")
    calls: list[Any] = []
    t = ApiTranscriber(MediaSettings(transcriber="api"),
                       client=httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(500))))
    t.cancel.set()
    with pytest.raises(TranscriptionCancelled):
        t.transcribe(audio)
    assert calls == []


def test_transcription_pool_is_bounded() -> None:
    assert pipeline_mod.TRANSCRIBE_WORKERS == 2 and pipeline_mod._TRANSCRIBE_POOL._max_workers == 2
    assert isinstance(LocalWhisper(MediaSettings()).cancel, threading.Event)
