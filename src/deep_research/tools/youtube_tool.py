"""Keyless YouTube transcript tool.

Input is either ``video`` (a YouTube URL or 11-char id) or ``query`` (free text;
video URLs/ids inside it are used directly, otherwise the public results page is
scanned for video ids). For each video the watch page's ``captionTracks`` are
read and the timed-text track (English preferred, manual before auto-generated)
is downloaded and flattened to text. Videos without captions are reported in
``errors`` rather than faked.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
import xml.etree.ElementTree as ET  # noqa: S405 - YouTube timed-text XML, no entity expansion
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from deep_research.domain.modality import Modality
from deep_research.tools.definition import ToolCapability, ToolDefinition
from deep_research.tools.source_common import (
    SourceSearchInput,
    SourceSearchOutput,
    clean,
    error_kind,
    fail,
    hit,
    ok,
)
from deep_research.tools.tool import Tool, ToolRequest, ToolResult

YOUTUBE_TOOL_ID = "youtube_transcript"
logger = logging.getLogger(__name__)
WATCH_URL = "https://www.youtube.com/watch"
RESULTS_URL = "https://www.youtube.com/results"
_BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_ID_IN_TEXT = re.compile(r"(?:youtu\.be/|[?&]v=|/shorts/|/embed/)([A-Za-z0-9_-]{11})")
_ID_IN_RESULTS = re.compile(r'"videoId":"([A-Za-z0-9_-]{11})"')
_TRACKS = re.compile(r'"captionTracks":(\[.*?\])')
_TITLE = re.compile(r'<meta name="title" content="([^"]*)"')


def video_id(value: str) -> str | None:
    """Return the video id from a URL or bare id, else None."""
    value = value.strip()
    if _ID.match(value):
        return value
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if host.endswith("youtu.be"):
        candidate = parsed.path.lstrip("/")[:11]
        return candidate if _ID.match(candidate) else None
    if host.endswith("youtube.com"):
        v = parse_qs(parsed.query).get("v", [""])[0]
        if _ID.match(v):
            return v
        match = _ID_IN_TEXT.search(value)
        return match.group(1) if match else None
    return None


class YouTubeTranscriptInput(SourceSearchInput):
    query: str = ""
    video: str = ""
    limit: int = 2


class YouTubeTranscriptTool(Tool):
    def __init__(self, *, http_client: httpx.AsyncClient | None = None, timeout: float = 15.0,
                 max_chars: int = 6_000, preferred_language: str = "en",
                 use_transcript_api: bool | None = None) -> None:
        self._client = http_client
        # youtube-transcript-api uses YouTube's current player API (the old timedtext baseUrl now needs a
        # PO token and returns empty). Default on for real use; injected test clients keep the httpx path.
        self._use_api = (http_client is None) if use_transcript_api is None else use_transcript_api
        self._timeout = timeout
        self._max_chars = max_chars
        self._lang = preferred_language

    @property
    def name(self) -> str:
        return YOUTUBE_TOOL_ID

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            identifier=YOUTUBE_TOOL_ID, name="YouTube Transcript",
            description="Find YouTube videos and read their public caption transcripts (keyless).",
            modality=Modality.VIDEO, capabilities=[ToolCapability.TRANSCRIBE, ToolCapability.SEARCH],
            input_schema=YouTubeTranscriptInput, output_schema=SourceSearchOutput,
            cost_class="FREE", network_required=True, auth_required=False, reliability="LOW",
            metadata={"source_type": "video"},
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, request: ToolRequest) -> ToolResult:
        start = time.monotonic()
        params = request.parameters
        raw_video, raw_query = params.get("video"), params.get("query")
        video = raw_video.strip() if isinstance(raw_video, str) else ""
        query = raw_query.strip() if isinstance(raw_query, str) else ""
        raw_limit = params.get("limit", 2)
        limit = max(1, min(raw_limit if isinstance(raw_limit, int) and not isinstance(raw_limit, bool) else 2, 5))
        if not video and not query:
            return fail(YOUTUBE_TOOL_ID, start, "Provide 'video' (URL/id) or 'query'.", "invalid_input")
        client = self._client or httpx.AsyncClient(follow_redirects=True)
        errors: dict[str, str] = {}
        try:
            ids = [video_id(video)] if video else _ids_in_text(query)
            if video and ids == [None]:
                return fail(YOUTUBE_TOOL_ID, start, "Not a YouTube URL or video id.", "invalid_input")
            if not ids:
                try:
                    ids = await self._search(client, query, limit)
                except (httpx.HTTPError, ValueError) as exc:
                    logger.warning("YouTube search failed (%s); falling back to web search", error_kind(exc))
                    return fail(YOUTUBE_TOOL_ID, start, "YouTube search failed.", error_kind(exc))
            rows = await self._transcripts(client, [i for i in ids if i][:limit], errors)
        finally:
            if self._client is None:
                await client.aclose()
        if not rows:
            logger.warning("YouTube: no transcript for %d video(s) %s; falling back to web search", len(errors), errors)
            return fail(YOUTUBE_TOOL_ID, start, "No captions available for the requested video(s); falling back to web search.",
                        "no_transcript", provider_errors=errors)
        return ok(YOUTUBE_TOOL_ID, start, rows, "youtube_captions", errors)

    async def _transcripts(self, client: httpx.AsyncClient, ids: list[str],
                           errors: dict[str, str]) -> list[dict[str, str]]:
        """Fetch each video's transcript; failures are logged and recorded in ``errors``."""
        rows: list[dict[str, str]] = []
        for vid in ids:
            if self._use_api:
                api_row = await self._api_transcript(client, vid, errors)
                if api_row is not None:
                    rows.append(api_row)
                    continue
            try:
                row = await self._transcript(client, vid)
            except _NoCaptions as exc:
                logger.warning("YouTube %s: no captions available (%s)", vid, exc)
                errors[vid] = "no_captions"
                continue
            except httpx.HTTPStatusError as exc:
                logger.warning("YouTube %s: HTTP %s fetching %s", vid, exc.response.status_code,
                               "caption track" if "timedtext" in str(exc.request.url) else "watch page")
                errors[vid] = error_kind(exc)
                continue
            except (httpx.HTTPError, ValueError, KeyError, TypeError, ET.ParseError) as exc:
                logger.warning("YouTube %s: transcript failed (%s)", vid, error_kind(exc))
                errors[vid] = error_kind(exc)
                continue
            rows.append(row)
        return rows

    async def _api_transcript(self, client: httpx.AsyncClient, vid: str,
                              errors: dict[str, str]) -> dict[str, str] | None:
        """Transcript via youtube-transcript-api (None when unavailable, so the legacy path runs)."""
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
        except ImportError:
            return None
        langs = list(dict.fromkeys([self._lang, "en", "en-US", "en-GB"]))

        def fetch() -> tuple[str, str, bool]:
            api = YouTubeTranscriptApi()
            listing = api.list(vid)
            try:
                tr = listing.find_transcript(langs)
            except Exception:  # noqa: BLE001 - no preferred language: take any (translated to English if possible)
                tr = next(iter(listing))
                if tr.is_translatable and not tr.language_code.startswith("en"):
                    tr = tr.translate("en")
            fetched = tr.fetch()
            text = clean(" ".join(s.text.replace("\n", " ") for s in fetched.snippets))
            return text, str(tr.language_code), bool(tr.is_generated)

        try:
            text, lang, generated = await asyncio.to_thread(fetch)
        except Exception as exc:  # noqa: BLE001 - library raises many types (disabled, unavailable, blocked)
            logger.info("youtube-transcript-api %s: %s", vid, type(exc).__name__)
            errors[f"{vid}:api"] = type(exc).__name__
            return None
        if not text:
            return None
        title = f"YouTube video {vid}"
        try:
            page = await client.get(WATCH_URL, params={"v": vid}, timeout=self._timeout,
                                    headers={"User-Agent": _BROWSER_UA, "Accept-Language": "en"})
            m = _TITLE.search(page.text)
            if m and m.group(1).strip():
                title = clean(html.unescape(m.group(1)))
        except httpx.HTTPError:
            pass
        return hit(title=title, url=f"https://www.youtube.com/watch?v={vid}", snippet=text[:300],
                   content=text[: self._max_chars], source_type="video", provider="youtube",
                   video_id=vid, language=lang, caption_kind="auto" if generated else "manual")

    async def _search(self, client: httpx.AsyncClient, query: str, limit: int) -> list[str | None]:
        response = await client.get(RESULTS_URL, params={"search_query": query},
                                    headers={"User-Agent": _BROWSER_UA, "Accept-Language": "en"},
                                    timeout=self._timeout)
        response.raise_for_status()
        found: list[str | None] = []
        for vid in _ID_IN_RESULTS.findall(response.text):
            if vid not in found:
                found.append(vid)
            if len(found) >= limit * 2:  # some will lack captions
                break
        return found

    async def _transcript(self, client: httpx.AsyncClient, vid: str) -> dict[str, str]:
        page = await client.get(WATCH_URL, params={"v": vid}, timeout=self._timeout,
                                headers={"User-Agent": _BROWSER_UA, "Accept-Language": "en"})
        page.raise_for_status()
        match = _TRACKS.search(page.text)
        if not match:
            raise _NoCaptions("watch page lists no caption tracks (or a consent/bot page was served)")
        try:
            tracks: list[dict[str, Any]] = json.loads(match.group(1))
        except ValueError as exc:
            raise _NoCaptions("caption track list unreadable") from exc
        track = _pick_track(tracks, self._lang)
        if track is None:
            raise _NoCaptions("no usable caption track")
        caption = await client.get(str(track["baseUrl"]).replace("\\u0026", "&"), timeout=self._timeout,
                                   headers={"User-Agent": _BROWSER_UA})
        caption.raise_for_status()
        if not caption.text.strip():
            raise _NoCaptions("caption download was empty")
        try:
            text = _caption_text(caption.text)
        except ET.ParseError as exc:
            raise _NoCaptions("caption download was not readable XML") from exc
        if not text:
            raise _NoCaptions("caption track has no text")
        title_match = _TITLE.search(page.text)
        title = clean(title_match.group(1)) if title_match else f"YouTube video {vid}"
        return hit(title=title, url=f"https://www.youtube.com/watch?v={vid}", snippet=text[:300],
                   content=text[: self._max_chars], source_type="video", provider="youtube",
                   video_id=vid, language=str(track.get("languageCode") or ""),
                   caption_kind="auto" if track.get("kind") == "asr" else "manual")


class _NoCaptions(Exception):
    """The video has no caption text we can read (not a transport error)."""


def _ids_in_text(text: str) -> list[str | None]:
    found: list[str | None] = []
    for token in text.split():
        vid = video_id(token)
        if vid and vid not in found and (("youtu" in token) or token == text.strip()):
            found.append(vid)
    for vid in _ID_IN_TEXT.findall(text):
        if vid not in found:
            found.append(vid)
    return found


def _pick_track(tracks: list[dict[str, Any]], lang: str) -> dict[str, Any] | None:
    usable = [t for t in tracks if isinstance(t, dict) and t.get("baseUrl")]
    if not usable:
        return None

    def rank(t: dict[str, Any]) -> tuple[int, int]:
        code = str(t.get("languageCode") or "")
        return (0 if code.split("-")[0] == lang else 1, 1 if t.get("kind") == "asr" else 0)

    return sorted(usable, key=rank)[0]


def _caption_text(xml_text: str) -> str:
    root = ET.fromstring(xml_text)  # noqa: S314 - see import note
    parts = [clean("".join(node.itertext())) for node in root.iter() if node.tag in ("text", "p")]
    return " ".join(p for p in parts if p)
