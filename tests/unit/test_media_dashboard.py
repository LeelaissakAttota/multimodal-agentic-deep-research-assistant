"""Dashboard side of audio/video: uploads, YouTube links, honest status (no network)."""

from __future__ import annotations

import httpx
import pytest

from deep_research.dashboard import core


def test_build_attachment_accepts_audio_and_video() -> None:
    a = core.build_attachment("talk.mp3", b"ID3data")
    v = core.build_attachment("clip.MOV", b"\x00\x00\x00\x14ftypqt")
    assert a["kind"] == "media" and a["data"].startswith("data:audio/mpeg;base64,")
    assert v["kind"] == "media" and v["data"].startswith("data:video/quicktime;base64,")
    with pytest.raises(ValueError, match="audio/video"):
        core.build_attachment("x.exe", b"MZ")


def test_youtube_link_becomes_media_attachment_without_download() -> None:
    def no_network(request: httpx.Request) -> httpx.Response:
        raise AssertionError("YouTube links must not be downloaded by the dashboard")

    att = core.attachment_from_url("https://www.youtube.com/watch?v=abcdefghijk",
                                   transport=httpx.MockTransport(no_network), validator=lambda u: True)
    assert att == {"kind": "media", "name": "https://www.youtube.com/watch?v=abcdefghijk",
                   "data": "https://www.youtube.com/watch?v=abcdefghijk"}
    with pytest.raises(ValueError, match="private"):
        core.attachment_from_url("https://youtu.be/abcdefghijk", validator=lambda u: False)
    assert core.is_media_link("https://m.youtube.com/watch?v=x") and not core.is_media_link("https://notyoutube.com/x")


def test_linked_audio_file_is_downloaded_as_media() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-type": "audio/mpeg"},
                                                             content=b"ID3abc"))
    att = core.attachment_from_url("https://example.org/pod/ep1", transport=transport, validator=lambda u: True)
    assert att["kind"] == "media" and att["name"] == "ep1.mp3"


def test_page_label_shows_mm_ss_for_media() -> None:
    assert core.page_label("01:15") == " at 01:15" and core.page_label(3) == " p.3" and core.page_label(None) == ""


def test_media_status_off_by_default_and_on_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    status = core.media_status()
    assert status["state"] == "off" and not status["ready"] and "MDRA_MEDIA_ENABLED" in status["line"]
    monkeypatch.setenv("MDRA_MEDIA_ENABLED", "true")
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    status = core.media_status()
    assert status["state"] == "not installed" and "faster-whisper" in status["line"] and ".[media]" in status["line"]
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    assert core.media_status()["state"] == "on"


def test_tool_catalog_and_knowledge_count_media() -> None:
    from deep_research.core.config import Settings

    rows = {t["name"]: t for t in core.tool_catalog(Settings(research_mode="offline"), None, "offline")}
    assert rows["Audio Analysis"]["state"] == "off" and "coming soon" not in rows["Video Analysis"]["detail"]
    runs = [{"run_id": "r1", "params": {"attachments": [{"kind": "media", "name": "talk.mp3"},
                                                        {"kind": "pdf", "name": "a.pdf"}]}}]
    kb = core.knowledge(runs)
    assert kb["videos"] == 1 and kb["documents"] == 1
