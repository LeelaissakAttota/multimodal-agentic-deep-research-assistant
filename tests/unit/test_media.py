"""Media (audio/video/YouTube) analysis. Network, whisper and vision models are all faked here;
only the ffmpeg keyframe test touches a real binary (skipped when ffmpeg is absent)."""

from __future__ import annotations

import asyncio
import base64
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from deep_research.core.attachment_context import attachment_entry, select_attachment_sources
from deep_research.domain.research_request import ATTACHMENT_TOOLS, ResearchAttachment
from deep_research.media import frames as frames_mod
from deep_research.media import pipeline as pipeline_mod
from deep_research.media import remote
from deep_research.media.config import MediaSettings
from deep_research.media.errors import MediaError, MediaUnavailable
from deep_research.media.pipeline import MediaPipeline
from deep_research.media.timecode import Segment, fmt_ts, windows
from deep_research.media.transcribe import ApiTranscriber, LocalWhisper, is_free_endpoint, parse_api_response
from deep_research.tools.media_tool import DISABLED_MESSAGE, MediaAnalysisTool, at_time
from deep_research.tools.tool import ToolRequest

AUDIO = "data:audio/mpeg;base64," + base64.b64encode(b"ID3fake-mp3-bytes").decode()
VIDEO = "data:video/mp4;base64," + base64.b64encode(b"\x00\x00\x00\x18ftypmp42fake").decode()
SPEECH = [Segment(0.0, 4.0, "Coral reefs bleach when water is too warm."),
          Segment(4.0, 9.5, "Heat stress above one degree for weeks is the trigger."),
          Segment(65.0, 70.0, "Recovery takes ten to fifteen years.")]


class FakeTranscriber:
    name = "fake-whisper"

    def __init__(self, segments: list[Segment]) -> None:
        self.segments = segments
        self.paths: list[Path] = []

    def transcribe(self, path: Path) -> list[Segment]:
        assert path.exists()
        self.paths.append(path)
        return self.segments


class FakeVision:
    def __init__(self, text: str = "A slide titled 'Bleaching thresholds' with a line chart.") -> None:
        self.text = text
        self.calls = 0

    async def generate(self, request: Any) -> Any:
        self.calls += 1
        assert request.image_data.startswith("data:image/jpeg;base64,")
        return type("R", (), {"text": self.text, "model_id": "fake/vision:free"})()


def enabled(**kw: Any) -> MediaSettings:
    return MediaSettings(**{"enabled": True, **kw})


def run(tool: MediaAnalysisTool, data: str, title: str | None = "Reef talk") -> Any:
    return asyncio.run(tool.execute(ToolRequest(tool_name="media_analyzer",
                                                parameters={"media_data": data, "source_title": title})))


@pytest.fixture
def no_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(frames_mod, "frame_backend", lambda: None)
    monkeypatch.setattr(pipeline_mod, "whisper_available", lambda: True)


# ----- timecodes ---------------------------------------------------------------------------------

def test_fmt_ts_and_windows_group_speech_and_keep_frames() -> None:
    assert fmt_ts(0) == "00:00" and fmt_ts(65.9) == "01:05" and fmt_ts(3725) == "1:02:05" and fmt_ts(-3) == "00:00"
    pages = windows([*SPEECH, Segment(30.0, 30.0, "a chart", kind="frame")], 60)
    assert [p["page"] for p in pages] == ["00:00", "00:30", "01:05"]
    assert pages[0]["text"].startswith("[00:00-00:09] Coral reefs") and "Heat stress" in pages[0]["text"]
    assert pages[1]["text"] == "[frame at 00:30] a chart" and pages[1]["kind"] == "frame"


def test_sample_times_are_centred() -> None:
    assert frames_mod.sample_times(60, 3) == [10.0, 30.0, 50.0]
    assert frames_mod.sample_times(0, 3) == [] and frames_mod.sample_times(10, 0) == []


# ----- captions ----------------------------------------------------------------------------------

def test_parse_vtt_strips_tags_and_rolling_duplicates() -> None:
    vtt = ("WEBVTT\n\n00:00:01.000 --> 00:00:03.500\nHello <c>reef</c> world\n\n"
           "00:00:03.500 --> 00:00:05.000\nHello reef world\n\n"
           "00:00:05.000 --> 00:00:07.000\nHello reef world and fish\n\n"
           "01:02:03.000 --> 01:02:04.000\nlate line\n")
    segs = remote.parse_vtt(vtt)
    assert [(s.start, s.text) for s in segs] == [(1.0, "Hello reef world"), (5.0, "and fish"), (3723.0, "late line")]


def test_parse_json3() -> None:
    raw = (b'{"events":[{"tStartMs":1500,"dDurationMs":2000,"segs":[{"utf8":"warm "},{"utf8":"water"}]},'
           b'{"tStartMs":4000,"segs":[{"utf8":"\\n"}]}]}')
    assert remote.parse_captions(raw, "json3") == [Segment(1.5, 3.5, "warm water")]
    with pytest.raises(MediaError):
        remote.parse_json3("not json")


def test_pick_caption_prefers_manual_then_original_auto_never_translations() -> None:
    info = {"subtitles": {"de": [{"ext": "vtt", "url": "u-de"}], "en": [{"ext": "srv1", "url": "x"},
                                                                         {"ext": "vtt", "url": "u-en"}]},
            "automatic_captions": {"en": [{"ext": "json3", "url": "a-en"}]}}
    assert remote.pick_caption(info, None) == ("u-en", "vtt", False)
    auto_only = {"language": "fr", "automatic_captions": {"de": [{"ext": "vtt", "url": "a-de"}],
                                                           "fr-orig": [{"ext": "json3", "url": "a-fr"}]}}
    assert remote.pick_caption(auto_only, None) == ("a-fr", "json3", True)
    assert remote.pick_caption({"automatic_captions": {"de": [{"ext": "vtt", "url": "a-de"}]}}, "en") is None
    assert remote.pick_caption({}, None) is None


# ----- transcribers ------------------------------------------------------------------------------

def test_free_endpoint_guard_refuses_paid_apis() -> None:
    assert is_free_endpoint("http://127.0.0.1:8000/v1", "whisper-1")
    assert is_free_endpoint("http://localhost:9000/v1", "x") and is_free_endpoint("https://box.local/v1", "x")
    assert is_free_endpoint("https://openrouter.ai/api/v1", "openai/whisper:free")
    assert not is_free_endpoint("https://api.openai.com/v1", "whisper-1")
    with pytest.raises(MediaError) as err:
        ApiTranscriber(MediaSettings(transcriber="api", api_base="https://api.openai.com/v1"))
    assert err.value.kind == "paid_endpoint_refused"


def test_api_transcriber_posts_multipart_and_parses_segments(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["auth"], seen["body"] = str(request.url), request.headers.get("authorization"), request.content
        return httpx.Response(200, json={"text": "x", "segments": [{"start": 1.0, "end": 2.5, "text": " Hi "}]})

    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"ID3")
    settings = MediaSettings(transcriber="api", api_key=SecretStr("sk-test-not-real"), language="en")
    t = ApiTranscriber(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert t.transcribe(audio) == [Segment(1.0, 2.5, "Hi")]
    assert seen["url"] == "http://127.0.0.1:8000/v1/audio/transcriptions"
    assert seen["auth"] == "Bearer sk-test-not-real" and b"verbose_json" in seen["body"] and b'name="language"' in seen["body"]
    assert "sk-test" not in repr(settings)


def test_api_transcriber_errors_are_clear(tmp_path: Path) -> None:
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    t = ApiTranscriber(MediaSettings(transcriber="api"),
                       client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    with pytest.raises(MediaError) as err:
        t.transcribe(audio)
    assert err.value.kind == "transcription_api_error"
    assert parse_api_response({"text": "whole", "duration": 12}) == [Segment(0.0, 12.0, "whole")]


def test_local_whisper_missing_gives_install_hint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import builtins

    real = builtins.__import__

    def fake_import(name: str, *a: Any, **k: Any) -> Any:
        if name == "faster_whisper":
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(MediaUnavailable) as err:
        LocalWhisper(MediaSettings()).transcribe(tmp_path / "x.mp3")
    assert 'pip install -e ".[media]"' in str(err.value) and "faster-whisper" in str(err.value)


# ----- the tool ----------------------------------------------------------------------------------

def test_off_by_default() -> None:
    assert MediaSettings().enabled is False
    result = run(MediaAnalysisTool(), AUDIO)
    assert not result.success and result.error == DISABLED_MESSAGE
    assert result.metadata["failure_kind"] == "media_disabled" and ".[media]" in DISABLED_MESSAGE


def test_missing_whisper_is_reported_not_faked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(frames_mod, "frame_backend", lambda: None)
    monkeypatch.setattr(pipeline_mod, "whisper_available", lambda: False)
    result = run(MediaAnalysisTool(settings=enabled()), AUDIO)
    assert not result.success and result.metadata["failure_kind"] == "media_unavailable"
    assert ".[media]" in str(result.error)


def test_audio_upload_gives_timestamped_pages_and_citations(no_frames: None) -> None:
    fake = FakeTranscriber(SPEECH)
    settings = enabled()
    tool = MediaAnalysisTool(settings=settings, pipeline=MediaPipeline(settings, transcriber_factory=lambda s: fake))
    result = run(tool, AUDIO)
    assert result.success, result.error
    out = result.output
    assert fake.paths[0].suffix == ".mp3" and not fake.paths[0].exists()  # temp file cleaned up
    assert out["media_kind"] == "audio" and out["transcript_source"] == "fake-whisper" and out["timestamps"] is True
    assert [p["page"] for p in out["pages"]] == ["00:00", "01:05"] and out["frames_captioned"] == 0
    entry = attachment_entry("media_analyzer", out)
    assert entry is not None and entry["kind"] == "audio"
    sources, evidence = select_attachment_sources([entry], "how long does reef recovery take")
    labels = [s.title for s in sources]
    assert "Reef talk (attached audio, at 01:05)" in labels and "Reef talk (attached audio, at 00:00)" in labels
    assert all(s.metadata["page"] in ("00:00", "01:05") for s in sources) and evidence[0].metadata["attachment"]


def test_video_keyframes_are_captioned_by_the_vision_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline_mod, "whisper_available", lambda: True)
    monkeypatch.setattr(frames_mod, "frame_backend", lambda: "ffmpeg")
    monkeypatch.setattr(frames_mod, "probe", lambda p: {"duration": 120.0, "has_video": True})
    monkeypatch.setattr(frames_mod, "extract_frames", lambda p, ts: [(t, b"\xff\xd8jpeg") for t in ts])
    vision = FakeVision()
    settings = enabled(keyframes=2)
    pipe = MediaPipeline(settings, vision_gateway=vision, transcriber_factory=lambda s: FakeTranscriber(SPEECH))
    out = run(MediaAnalysisTool(settings=settings, pipeline=pipe), VIDEO).output
    assert out["media_kind"] == "video" and out["frames_captioned"] == 2 and vision.calls == 2
    assert out["frame_model"] == "fake/vision:free" and out["duration"] == "02:00"
    frames = [p for p in out["pages"] if p["kind"] == "frame"]
    assert [p["page"] for p in frames] == ["00:30", "01:30"] and "Bleaching thresholds" in frames[0]["text"]


def test_video_without_vision_model_notes_it_and_invents_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline_mod, "whisper_available", lambda: True)
    monkeypatch.setattr(frames_mod, "frame_backend", lambda: "pyav")
    monkeypatch.setattr(frames_mod, "probe", lambda p: {"duration": 20.0, "has_video": True})
    settings = enabled()
    pipe = MediaPipeline(settings, transcriber_factory=lambda s: FakeTranscriber(SPEECH[:1]))
    out = run(MediaAnalysisTool(settings=settings, pipeline=pipe), VIDEO).output
    assert out["frames_captioned"] == 0 and all(p["kind"] == "speech" for p in out["pages"])
    assert "no free vision model" in out["note"]


def test_silent_media_fails_honestly(no_frames: None) -> None:
    settings = enabled()
    pipe = MediaPipeline(settings, transcriber_factory=lambda s: FakeTranscriber([]))
    result = run(MediaAnalysisTool(settings=settings, pipeline=pipe), AUDIO)
    assert not result.success and result.metadata["failure_kind"] == "empty_media" and "no speech" in str(result.error)


def test_too_long_media_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(frames_mod, "frame_backend", lambda: "ffmpeg")
    monkeypatch.setattr(frames_mod, "probe", lambda p: {"duration": 7200.0, "has_video": False})
    settings = enabled(max_duration_seconds=3600)
    result = run(MediaAnalysisTool(settings=settings, pipeline=MediaPipeline(settings)), AUDIO)
    assert result.metadata["failure_kind"] == "media_too_long"


def test_bad_mime_rejected() -> None:
    result = run(MediaAnalysisTool(settings=enabled()), "data:text/html;base64,PGI+")
    assert not result.success


def test_youtube_captions_used_without_downloading(monkeypatch: pytest.MonkeyPatch) -> None:
    info = {"title": "Reef lecture", "duration": 600, "vcodec": "avc1",
            "subtitles": {"en": [{"ext": "vtt", "url": "https://captions.example/en.vtt"}]}}
    monkeypatch.setattr(remote, "fetch_info", lambda url: info)
    monkeypatch.setattr(remote, "download_caption",
                        lambda url: b"WEBVTT\n\n00:01:15.000 --> 00:01:20.000\nWarm water bleaches coral.\n")

    def no_download(*a: Any, **k: Any) -> Path:
        raise AssertionError("must not download when captions exist")

    monkeypatch.setattr(remote, "download", no_download)
    settings = enabled()
    tool = MediaAnalysisTool(settings=settings, url_validator=lambda u: True)
    out = run(tool, "https://www.youtube.com/watch?v=abcdefghijk", None).output
    assert out["transcript_source"] == "captions (manual)" and out["media_kind"] == "video"
    page = out["pages"][0]
    assert page["page"] == "01:15" and page["url"] == "https://www.youtube.com/watch?v=abcdefghijk&t=75s"
    assert out["source_objects"][0].title == "Reef lecture"
    assert "MDRA_MEDIA_YOUTUBE_FRAMES" in out["note"]
    entry = attachment_entry("media_analyzer", out)
    sources, _ = select_attachment_sources([entry], "warm water")
    assert sources[0].title == "Reef lecture (attached video, at 01:15)" and str(sources[0].url).endswith("t=75s")


def test_youtube_without_captions_downloads_audio_and_transcribes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote, "fetch_info", lambda url: {"title": "Talk", "duration": 30, "vcodec": "none"})
    monkeypatch.setattr(pipeline_mod, "whisper_available", lambda: True)
    monkeypatch.setattr(frames_mod, "frame_backend", lambda: None)

    def fake_download(url: str, out_dir: Path, settings: MediaSettings, *, video: bool) -> Path:
        assert video is False
        path = out_dir / "audio.webm"
        path.write_bytes(b"x")
        return path

    monkeypatch.setattr(remote, "download", fake_download)
    settings = enabled()
    pipe = MediaPipeline(settings, transcriber_factory=lambda s: FakeTranscriber(SPEECH[:1]))
    out = run(MediaAnalysisTool(settings=settings, pipeline=pipe, url_validator=lambda u: True),
              "https://youtu.be/abcdefghijk").output
    assert out["transcript_source"] == "fake-whisper" and out["media_kind"] == "audio"


def test_private_media_links_blocked_before_any_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote, "fetch_info", lambda url: pytest.fail("fetched"))
    result = run(MediaAnalysisTool(settings=enabled(), url_validator=lambda u: False), "http://127.0.0.1/v.mp4")
    assert result.metadata["failure_kind"] == "blocked_url"


def test_at_time_replaces_existing_t() -> None:
    assert at_time("https://youtu.be/abc?t=5", 61.7) == "https://youtu.be/abc?t=61s"


def test_ytdlp_missing_gives_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real = builtins.__import__
    monkeypatch.setattr(builtins, "__import__",
                        lambda name, *a, **k: (_ for _ in ()).throw(ImportError(name)) if name == "yt_dlp"
                        else real(name, *a, **k))
    with pytest.raises(MediaUnavailable) as err:
        remote.fetch_info("https://youtu.be/abcdefghijk")
    assert "yt-dlp" in str(err.value) and ".[media]" in str(err.value)


# ----- request model / planner / CLI -------------------------------------------------------------

def test_media_attachment_kind_and_planner_task() -> None:
    from deep_research.core.agents.planning_agent import PlanningAgent  # noqa: F401 - import check

    assert ATTACHMENT_TOOLS["media"] == ("media_analyzer", "media_data")
    assert ResearchAttachment(kind="media", data="https://youtu.be/abcdefghijk").data.startswith("https")
    assert ResearchAttachment(kind="media", data=AUDIO).kind == "media"
    for bad in ("https://x.org/a b", "ftp://x.org/a.mp3"):
        with pytest.raises(ValueError):
            ResearchAttachment(kind="media", data=bad)


def test_cli_maps_media_files_and_youtube_links(tmp_path: Path) -> None:
    from deep_research.cli import attachment_for, supported_suffixes

    assert attachment_for("https://www.youtube.com/watch?v=abcdefghijk").kind == "media"
    assert attachment_for("https://example.org/page").kind == "url"
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftyp")
    att = attachment_for(str(clip))
    assert att.kind == "media" and att.data.startswith("data:video/mp4;base64,") and ".mp3" in supported_suffixes()


def test_offline_run_with_media_off_fails_task_clearly() -> None:
    from deep_research.api.research_service import ResearchApplication, ResearchSubmission
    from deep_research.core.config import Settings

    sub = ResearchSubmission(objective="What does the talk say about coral bleaching?",
                             attachments=[ResearchAttachment(kind="media", data=AUDIO, name="talk.mp3")])
    resp = asyncio.run(ResearchApplication(Settings(research_mode="offline")).submit(sub))
    assert resp.status == "failed" and "MDRA_MEDIA_ENABLED" in str(resp.error)


# ----- RAG: timestamps survive into the index ----------------------------------------------------

def test_rag_chunks_keep_mm_ss_from_media_citations() -> None:
    from deep_research.rag.chunking import chunk_report

    report = {"content": "Bleaching starts with heat stress [1].",
              "citations": [{"number": 1, "kind": "attachment", "page": "01:15",
                             "title": "Reef lecture (attached video, at 01:15)",
                             "excerpt": "[01:15-01:20] Warm water bleaches coral."}]}
    chunks = chunk_report("run-1", report)
    source = [c for c in chunks if c.kind == "source"]
    assert source and "at 01:15" in source[0].text and "[01:15-01:20]" in source[0].text


# ----- real ffmpeg (no network, no model) --------------------------------------------------------

@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg not installed")
def test_real_ffmpeg_probe_and_keyframes(tmp_path: Path) -> None:
    clip = tmp_path / "test.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=4:size=320x240:rate=10",
                    "-pix_fmt", "yuv420p", str(clip)], check=True, timeout=60)
    meta = frames_mod.probe(clip)
    assert meta["has_video"] and 3.5 < meta["duration"] < 4.5
    shots = frames_mod.extract_frames(clip, frames_mod.sample_times(meta["duration"], 2))
    assert len(shots) == 2 and all(jpeg.startswith(b"\xff\xd8") for _, jpeg in shots)
    assert frames_mod.extract_audio(clip, tmp_path) == clip  # no audio stream -> original file kept


def test_transcriber_crash_becomes_clear_failure(no_frames: None) -> None:
    class Broken:
        name = "broken"

        def transcribe(self, path: Path) -> list[Segment]:
            raise TypeError("open() got an unexpected keyword argument")

    settings = enabled()
    result = run(MediaAnalysisTool(settings=settings, pipeline=MediaPipeline(settings, transcriber_factory=lambda s: Broken())),
                 AUDIO)
    assert result.metadata["failure_kind"] == "transcription_failed" and "TypeError" in str(result.error)
