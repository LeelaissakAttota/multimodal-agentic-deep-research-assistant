"""Testing Bot: phase 7 (B) media edge cases."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from deep_research.media import frames, remote
from deep_research.media.config import MediaSettings
from deep_research.media.errors import MediaError
from deep_research.media.pipeline import MediaPipeline
from deep_research.media.timecode import Segment, fmt_ts, windows
from deep_research.media.transcribe import ApiTranscriber, is_free_endpoint


@pytest.mark.parametrize(("sec", "want"), [(0, "00:00"), (59.999, "00:59"), (60, "01:00"), (3599.9, "59:59"),
                                           (3600, "1:00:00"), (90061, "25:01:01"), (-5, "00:00")])
def test_fmt_ts_edges(sec: float, want: str) -> None:
    assert fmt_ts(sec) == want


def test_media_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MDRA_MEDIA_ENABLED", raising=False)
    monkeypatch.setenv("DEEP_RESEARCH_ENV_FILE", "/nonexistent.env")
    assert MediaSettings(_env_file=None).enabled is False  # type: ignore[call-arg]


def test_windows_empty_and_frame_order() -> None:
    assert windows([], 60) == []
    pages = windows([Segment(5, 5, "f", kind="frame"), Segment(0, 2, "a")], 60)
    assert [p["kind"] for p in pages] == ["speech", "frame"]


@pytest.mark.parametrize("base", ["http://localhost:8000/v1", "http://127.0.0.1/v1", "http://[::1]/v1"])
def test_local_endpoints_allowed(base: str) -> None:
    assert is_free_endpoint(base, "whisper-1")


@pytest.mark.parametrize("base", ["https://api.openai.com/v1", "http://localhost.evil.com/v1",
                                  "http://127.0.0.1.nip.io/v1"])
def test_paid_endpoint_refused(base: str) -> None:
    assert not is_free_endpoint(base, "whisper-1")
    with pytest.raises(MediaError):
        ApiTranscriber(MediaSettings(transcriber="api", api_base=base, api_model="whisper-1"))


def test_free_suffix_only_on_free_provider() -> None:
    assert not is_free_endpoint("https://api.openai.com/v1", "whisper-1:free")


def test_sample_times_edges() -> None:
    assert frames.sample_times(0, 5) == [] and frames.sample_times(10, 0) == [] and frames.sample_times(-1, 3) == []
    assert frames.sample_times(10, 2) == [2.5, 7.5]


def test_ffmpeg_path_is_absolute_tmp_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[list[str]] = []

    class R:
        stdout = b"x"

    monkeypatch.setattr(frames.subprocess, "run", lambda cmd, **k: (seen.append(cmd), R())[1])
    frames._ffmpeg_frames(tmp_path / "-evil.mp4", [1.0])
    arg = seen[0][seen[0].index("-i") + 1]
    assert not arg.startswith("-")


def test_vtt_hours_and_dedupe() -> None:
    vtt = b"WEBVTT\n\n01:02:03.500 --> 01:02:05.000\nhello\n\n01:02:05.000 --> 01:02:06.000\nhello\n"
    segs = remote.parse_captions(vtt, "vtt")
    assert len(segs) == 1 and segs[0].start == pytest.approx(3723.5)


def test_json3_non_object_is_media_error() -> None:
    with pytest.raises(MediaError):
        remote.parse_captions(b"[]", "json3")


def test_caption_url_is_revalidated(monkeypatch: pytest.MonkeyPatch) -> None:
    fetched: list[str] = []
    info: dict[str, Any] = {"duration": 10, "vcodec": "none",
                            "subtitles": {"en": [{"ext": "vtt", "url": "http://169.254.169.254/latest/meta-data"}]}}
    monkeypatch.setattr(remote, "fetch_info", lambda url, validator=None: info)
    monkeypatch.setattr(remote, "download_caption",
                        lambda u: (fetched.append(u), b"WEBVTT\n\n00:00.000 --> 00:01.000\nhi\n")[1])
    pipe = MediaPipeline(MediaSettings(enabled=True, keyframes=0))
    try:
        asyncio.run(pipe.analyze_url("https://www.youtube.com/watch?v=x"))
    except MediaError:
        pass
    assert fetched == []
