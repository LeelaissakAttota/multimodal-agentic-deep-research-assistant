"""YouTube transcript tool, all network mocked."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from deep_research.domain.modality import Modality
from deep_research.tools.tool import ToolRequest
from deep_research.tools.youtube_tool import YOUTUBE_TOOL_ID, YouTubeTranscriptTool, video_id

VID = "dQw4w9WgXcQ"
VID2 = "abcdefghijk"
TRACKS = [
    {"baseUrl": "https://www.youtube.com/api/timedtext?v=X&lang=de", "languageCode": "de"},
    {"baseUrl": "https://www.youtube.com/api/timedtext?v=X&lang=en&kind=asr", "languageCode": "en", "kind": "asr"},
    {"baseUrl": "https://www.youtube.com/api/timedtext?v=X\\u0026lang=en", "languageCode": "en"},
]
WATCH = ('<html><meta name="title" content="Intro to Transformers &amp; Attention">'
         f'<script>var x = {{"captions":{{"captionTracks":{json.dumps(TRACKS)},"other":1}}}};</script></html>')
CAPTIONS = '<?xml version="1.0"?><transcript><text start="0">Hello &amp;amp; welcome</text><text start="1">to attention</text></transcript>'


def _client(calls: list[str], *, captions: bool = True) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/results":
            return httpx.Response(200, text=f'"videoId":"{VID}" "videoId":"{VID}" "videoId":"{VID2}"')
        if request.url.path == "/watch":
            return httpx.Response(200, text=WATCH if captions else "<html>no captions</html>")
        if request.url.path == "/api/timedtext":
            return httpx.Response(200, text=CAPTIONS)
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _run(tool: YouTubeTranscriptTool, **params: Any) -> Any:
    return asyncio.run(tool.execute(ToolRequest(tool_name=YOUTUBE_TOOL_ID, parameters=params)))


def test_video_id_parsing() -> None:
    assert video_id(VID) == VID
    assert video_id(f"https://www.youtube.com/watch?v={VID}&t=3") == VID
    assert video_id(f"https://youtu.be/{VID}") == VID
    assert video_id(f"https://www.youtube.com/shorts/{VID}") == VID
    assert video_id("https://example.com/watch?v=dQw4w9WgXcQ") is None
    assert video_id("not a video") is None


def test_definition() -> None:
    d = YouTubeTranscriptTool().get_definition()
    assert d.modality == Modality.VIDEO and d.cost_class == "FREE" and not d.auth_required


def test_transcript_from_url_prefers_manual_english() -> None:
    calls: list[str] = []
    result = _run(YouTubeTranscriptTool(http_client=_client(calls)), video=f"https://youtu.be/{VID}")
    assert result.success
    row = result.output.results[0]
    assert row["url"] == f"https://www.youtube.com/watch?v={VID}"
    assert row["title"] == "Intro to Transformers & Attention"
    assert row["content"] == "Hello & welcome to attention"
    assert row["source_type"] == "video" and row["caption_kind"] == "manual" and row["language"] == "en"
    assert any("lang=en" in c and "kind=asr" not in c for c in calls if "timedtext" in c)


def test_query_searches_and_dedupes_ids() -> None:
    calls: list[str] = []
    result = _run(YouTubeTranscriptTool(http_client=_client(calls)), query="transformer lecture", limit=2)
    assert result.success
    assert [r["video_id"] for r in result.output.results] == [VID, VID2]
    assert any("/results" in c for c in calls)


def test_query_containing_url_skips_search() -> None:
    calls: list[str] = []
    _run(YouTubeTranscriptTool(http_client=_client(calls)), query=f"summarise https://youtu.be/{VID} please")
    assert not any("/results" in c for c in calls)


def test_no_captions_is_honest_failure() -> None:
    result = _run(YouTubeTranscriptTool(http_client=_client([], captions=False)), video=VID)
    assert not result.success
    assert result.metadata["failure_kind"] == "no_transcript"
    assert result.metadata["provider_errors"] == {VID: "no_captions"}


def test_invalid_inputs() -> None:
    tool = YouTubeTranscriptTool(http_client=_client([]))
    assert _run(tool).metadata["failure_kind"] == "invalid_input"
    assert _run(tool, video="https://example.com/x").metadata["failure_kind"] == "invalid_input"


def _custom(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_empty_caption_download_is_no_captions_not_malformed(caplog: Any) -> None:
    import logging

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=WATCH if request.url.path == "/watch" else "")

    with caplog.at_level(logging.WARNING, logger="deep_research.tools.youtube_tool"):
        result = _run(YouTubeTranscriptTool(http_client=_custom(handler)), video=VID)
    assert not result.success and result.metadata["provider_errors"] == {VID: "no_captions"}
    assert "no captions available (caption download was empty)" in caplog.text
    assert "falling back to web search" in caplog.text
    assert "No captions available" in (result.error or "")


def test_watch_page_and_caption_http_failures_are_logged(caplog: Any) -> None:
    import logging

    def watch_down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    def caption_down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=WATCH) if request.url.path == "/watch" else httpx.Response(403)

    with caplog.at_level(logging.WARNING, logger="deep_research.tools.youtube_tool"):
        a = _run(YouTubeTranscriptTool(http_client=_custom(watch_down)), video=VID)
        b = _run(YouTubeTranscriptTool(http_client=_custom(caption_down)), video=VID)
    assert a.metadata["provider_errors"] == {VID: "http_429"}
    assert b.metadata["provider_errors"] == {VID: "http_403"}
    assert "HTTP 429 fetching watch page" in caplog.text and "HTTP 403 fetching caption track" in caplog.text


def test_unreadable_caption_xml_is_no_captions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=WATCH if request.url.path == "/watch" else "<not xml")

    result = _run(YouTubeTranscriptTool(http_client=_custom(handler)), video=VID)
    assert result.metadata["provider_errors"] == {VID: "no_captions"}


def test_transcript_api_path_used_first(monkeypatch):
    import asyncio
    import sys
    import types

    import httpx

    from deep_research.tools.tool import ToolRequest
    from deep_research.tools.youtube_tool import YOUTUBE_TOOL_ID, YouTubeTranscriptTool

    class Snip:
        def __init__(self, t):
            self.text = t

    class Tr:
        language_code, is_generated, is_translatable = "en", True, False

        def fetch(self):
            return types.SimpleNamespace(snippets=[Snip("hello"), Snip("coral\nworld")])

    class Listing:
        def find_transcript(self, langs):
            return Tr()

        def __iter__(self):
            return iter([Tr()])

    class Api:
        def list(self, vid):
            return Listing()

    monkeypatch.setitem(sys.modules, "youtube_transcript_api", types.SimpleNamespace(YouTubeTranscriptApi=Api))

    def handler(request):
        return httpx.Response(200, text='<meta name="title" content="Reef &amp; heat">')

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tool = YouTubeTranscriptTool(http_client=client, use_transcript_api=True)
    r = asyncio.run(tool.execute(ToolRequest(tool_name=YOUTUBE_TOOL_ID, parameters={"video": "dQw4w9WgXcQ"})))
    assert r.success
    row = r.output.results[0]
    assert row["content"] == "hello coral world" and row["title"] == "Reef & heat" and row["caption_kind"] == "auto"


def test_transcript_api_failure_falls_back(monkeypatch):
    import sys
    import types

    from deep_research.tools.youtube_tool import YouTubeTranscriptTool

    class Api:
        def list(self, vid):
            raise RuntimeError("blocked")

    monkeypatch.setitem(sys.modules, "youtube_transcript_api", types.SimpleNamespace(YouTubeTranscriptApi=Api))
    import asyncio

    import httpx

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="")))
    errors: dict[str, str] = {}
    out = asyncio.run(YouTubeTranscriptTool(http_client=client, use_transcript_api=True)._api_transcript(client, "dQw4w9WgXcQ", errors))
    assert out is None and errors == {"dQw4w9WgXcQ:api": "RuntimeError"}
