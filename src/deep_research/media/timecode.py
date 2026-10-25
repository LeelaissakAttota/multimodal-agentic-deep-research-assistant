"""Timestamped segments and the ``mm:ss`` labels citations use."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str
    kind: Literal["speech", "frame"] = "speech"


def fmt_ts(seconds: float) -> str:
    """``mm:ss`` (``h:mm:ss`` past an hour)."""

    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def windows(segments: list[Segment], seconds: int) -> list[dict[str, Any]]:
    """Group speech into ~``seconds`` windows; frames stay one page each. Sorted by start."""

    pages: list[dict[str, Any]] = []
    bucket: list[Segment] = []

    def flush() -> None:
        if bucket:
            start, end = bucket[0].start, bucket[-1].end
            text = " ".join(s.text.strip() for s in bucket if s.text.strip())
            if text:
                pages.append({"page": fmt_ts(start), "start": round(start, 2), "end": round(end, 2), "kind": "speech",
                              "text": f"[{fmt_ts(start)}-{fmt_ts(end)}] {text}"})
            bucket.clear()

    for seg in sorted((s for s in segments if s.kind == "speech"), key=lambda s: s.start):
        if bucket and seg.end - bucket[0].start > seconds:
            flush()
        bucket.append(seg)
    flush()
    for seg in segments:
        if seg.kind == "frame" and seg.text.strip():
            pages.append({"page": fmt_ts(seg.start), "start": round(seg.start, 2), "end": round(seg.end, 2),
                          "kind": "frame", "text": f"[frame at {fmt_ts(seg.start)}] {seg.text.strip()}"})
    pages.sort(key=lambda p: (float(p["start"]), p["kind"] != "speech"))
    return pages
