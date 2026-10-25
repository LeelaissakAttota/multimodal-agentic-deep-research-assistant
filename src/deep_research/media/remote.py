"""YouTube / remote media through yt-dlp: captions first, else download audio (and optional video)."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

from deep_research.media.config import MediaSettings
from deep_research.media.errors import MediaError, MediaUnavailable
from deep_research.media.timecode import Segment

_CAPTION_EXTS = ("json3", "vtt")
_VTT_TIME = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})\s*-->\s*(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})")
_TAG = re.compile(r"<[^>]+>")


def ytdlp_available() -> bool:
    return importlib.util.find_spec("yt_dlp") is not None


def _ydl(options: dict[str, Any]) -> Any:
    try:
        import yt_dlp
    except ImportError as exc:
        raise MediaUnavailable("yt-dlp for YouTube and remote media") from exc
    base = {"quiet": True, "no_warnings": True, "noplaylist": True, "noprogress": True}
    return yt_dlp.YoutubeDL({**base, **options})


def fetch_info(url: str) -> dict[str, Any]:
    try:
        with _ydl({"skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
            return dict(ydl.sanitize_info(info) or {})
    except MediaError:
        raise
    except Exception as exc:  # yt-dlp's DownloadError and friends
        raise MediaError(f"Could not read that media URL ({type(exc).__name__}: {_reason(exc)}).", "media_url_error") from exc


def pick_caption(info: dict[str, Any], language: str | None) -> tuple[str, str, bool] | None:
    """``(url, ext, auto_generated)``: manual subtitles beat auto captions; preferred language first."""

    want = (language or str(info.get("language") or "") or "en").lower()
    for auto, tracks in ((False, info.get("subtitles")), (True, info.get("automatic_captions"))):
        if not isinstance(tracks, dict) or not tracks:
            continue
        langs = sorted(tracks, key=lambda lang: (not lang.lower().startswith(want),
                                                 not lang.lower().startswith("en"), "-orig" not in lang, lang))
        for lang in langs:
            if auto and lang.lower().startswith(want) is False and not lang.endswith("-orig"):
                continue  # don't take machine *translations* of auto captions
            for ext in _CAPTION_EXTS:
                for fmt in tracks.get(lang) or []:
                    if isinstance(fmt, dict) and fmt.get("ext") == ext and fmt.get("url"):
                        return str(fmt["url"]), ext, auto
    return None


def download_caption(url: str) -> bytes:
    try:
        with _ydl({}) as ydl:
            return bytes(ydl.urlopen(url).read())
    except MediaError:
        raise
    except Exception as exc:
        raise MediaError(f"Caption download failed ({type(exc).__name__}).", "media_url_error") from exc


def parse_captions(raw: bytes, ext: str) -> list[Segment]:
    text = raw.decode("utf-8", errors="replace")
    return parse_json3(text) if ext == "json3" else parse_vtt(text)


def parse_json3(text: str) -> list[Segment]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise MediaError("Captions were not valid JSON.", "invalid_captions") from exc
    out: list[Segment] = []
    for event in data.get("events") or []:
        words = "".join(str(s.get("utf8") or "") for s in event.get("segs") or [] if isinstance(s, dict))
        words = " ".join(words.split())
        if words:
            start = float(event.get("tStartMs") or 0) / 1000
            out.append(Segment(start, start + float(event.get("dDurationMs") or 0) / 1000, words))
    return _dedupe(out)


def parse_vtt(text: str) -> list[Segment]:
    out: list[Segment] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = block.strip().split("\n")
        for i, line in enumerate(lines):
            match = _VTT_TIME.search(line)
            if match:
                g = match.groups()
                start = int(g[0] or 0) * 3600 + int(g[1]) * 60 + int(g[2]) + int(g[3]) / 1000
                end = int(g[4] or 0) * 3600 + int(g[5]) * 60 + int(g[6]) + int(g[7]) / 1000
                words = " ".join(_TAG.sub("", " ".join(lines[i + 1:])).split())
                if words:
                    out.append(Segment(start, end, words))
                break
    return _dedupe(out)


def _dedupe(segments: list[Segment]) -> list[Segment]:
    """Auto captions repeat the previous line as a rolling window; keep only new text."""

    out: list[Segment] = []
    for seg in segments:
        if out and seg.text == out[-1].text:
            continue
        if out and seg.text.startswith(out[-1].text) and seg.start - out[-1].start < 10:
            tail = seg.text[len(out[-1].text):].strip()
            if not tail:
                continue
            seg = Segment(seg.start, seg.end, tail)
        out.append(seg)
    return out


def download(url: str, out_dir: Path, settings: MediaSettings, *, video: bool) -> Path:
    """Download bestaudio (or a <=360p video) into ``out_dir``; size-capped."""

    fmt = "worst[height>=240][ext=mp4]/worst[ext=mp4]/worst" if video else "bestaudio/best"
    options = {"format": fmt, "outtmpl": str(out_dir / ("video.%(ext)s" if video else "audio.%(ext)s")),
               "max_filesize": settings.max_download_mb * 1024 * 1024}
    try:
        with _ydl(options) as ydl:
            info = ydl.extract_info(url, download=True)
            path = Path(ydl.prepare_filename(info))
    except MediaError:
        raise
    except Exception as exc:
        raise MediaError(f"Media download failed ({type(exc).__name__}: {_reason(exc)}).", "media_url_error") from exc
    if not path.exists():
        raise MediaError(f"Media was not downloaded (over {settings.max_download_mb} MB?).", "media_too_large")
    return path


def _reason(exc: Exception) -> str:
    """First line of yt-dlp's message (e.g. YouTube's bot check), without the ``ERROR:`` prefix."""

    text = str(exc).strip().split("\n")[0].removeprefix("ERROR:").strip()
    return text[:160] or "no detail"
