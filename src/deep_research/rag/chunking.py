"""Split one report into retrievable chunks that keep their ``[n]`` citations.

* report body: split at markdown headings into sections, then packed sentence by sentence into
  windows of about ``chunk_words`` words with ``overlap_words`` of trailing context carried over;
* claims: one chunk each, with the claim's own citations;
* sources: title + excerpt of every citation, windowed the same way and tagged with its number.

Only citation numbers that exist in the report's citation list are kept.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

_CITE = re.compile(r"\[(\d+(?:\s*[,\u2013-]\s*\d+)*)\]")
_HEADING = re.compile(r"^#{1,6}\s+(.*)$")
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")


@dataclass
class Chunk:
    chunk_id: str
    run_id: str
    kind: str  # "report" | "claim" | "source"
    ord: int
    text: str
    citations: list[int] = field(default_factory=list)
    title: str = ""
    section: str = ""
    doc_hash: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Chunk:
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


def cite_numbers(text: str, valid: set[int]) -> list[int]:
    out: set[int] = set()
    for marker in _CITE.findall(text):
        for part in re.split(r"\s*,\s*", marker):
            bounds = [int(x) for x in re.split(r"\s*[\u2013-]\s*", part) if x.strip().isdigit()]
            if len(bounds) == 2 and 0 < bounds[1] - bounds[0] < 20:
                out.update(range(bounds[0], bounds[1] + 1))
            else:
                out.update(bounds)
    return sorted(n for n in out if n in valid)


def report_hash(report: dict[str, Any]) -> str:
    """Stable fingerprint of the chunked fields: re-index only when it changes."""
    keep = {k: report.get(k) for k in ("content", "claim_scores", "citations")}
    return hashlib.sha256(json.dumps(keep, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def _split_long(sentence: str, size: int, overlap: int) -> list[str]:
    """Word windows of at most ``size`` words (consecutive windows share ``overlap`` words)."""
    words = sentence.split()
    if len(words) <= size:
        return [sentence]
    step = max(1, size - overlap)
    out = []
    for start in range(0, len(words), step):
        out.append(" ".join(words[start:start + size]))
        if start + size >= len(words):
            break
    return out


def _windows(sentences: list[str], size: int, overlap: int) -> list[str]:
    """Pack sentences into windows of at most ``size`` words (hard cap), carrying trailing context.

    Text without sentence boundaries (no ``.!?`` + capital, e.g. lowercase or non-Latin scripts such as
    Telugu) arrives as one huge "sentence" and is cut into overlapping word windows first.
    """
    out: list[str] = []
    cur: list[str] = []
    words = 0
    for sent in (piece for s in sentences for piece in _split_long(s, size, overlap)):
        n = len(sent.split())
        if cur and words + n > size:
            out.append(" ".join(cur))
            budget = min(overlap, size - n)  # carried context never pushes the next window over the cap
            tail: list[str] = []
            kept = 0
            for prev in reversed(cur):  # carry whole trailing sentences up to the budget
                if kept + len(prev.split()) > budget:
                    break
                tail.insert(0, prev)
                kept += len(prev.split())
            cur, words = tail, kept
        cur.append(sent)
        words += n
    if cur:
        out.append(" ".join(cur))
    return out


def _sections(content: str) -> list[tuple[str, list[str]]]:
    body = re.split(r"(?im)^#{1,3}\s*(sources|references)\s*$", content)[0]
    sections: list[tuple[str, list[str]]] = [("", [])]
    for raw in body.splitlines():
        line = raw.strip()
        head = _HEADING.match(line)
        if head:
            sections.append((head.group(1).strip(), []))
            continue
        line = line.lstrip("-*• ").strip()
        if not line or line.startswith(("|", "!")):
            continue
        sections[-1][1].extend(s.strip() for s in _SENT.split(line) if s.strip())
    return [s for s in sections if s[1]]


def chunk_report(run_id: str, report: dict[str, Any], *, title: str = "", chunk_words: int = 120,
                 overlap_words: int = 30) -> list[Chunk]:
    valid = {int(c["number"]) for c in report.get("citations") or []
             if isinstance(c, dict) and str(c.get("number", "")).isdigit()}
    doc = report_hash(report)
    overlap = min(overlap_words, chunk_words // 2)
    out: list[Chunk] = []

    def add(kind: str, text: str, cites: list[int], section: str = "") -> None:
        text = " ".join(text.split())
        if not text:
            return
        cid = hashlib.sha1(f"{run_id}|{kind}|{len(out)}|{text}".encode()).hexdigest()[:20]
        out.append(Chunk(cid, run_id, kind, len(out), text, cites, title, section, doc))

    for section, sentences in _sections(str(report.get("content") or "")):
        for window in _windows(sentences, chunk_words, overlap):
            add("report", window, cite_numbers(window, valid), section)
    for claim in report.get("claim_scores") or []:
        if isinstance(claim, dict) and claim.get("text"):
            cites = sorted({int(n) for n in claim.get("citations") or [] if str(n).isdigit() and int(n) in valid})
            add("claim", str(claim["text"]), cites, str(claim.get("section") or ""))
    for cit in report.get("citations") or []:
        if isinstance(cit, dict) and str(cit.get("number", "")).isdigit():
            number = int(cit["number"])
            name = str(cit.get("title") or "").strip()
            excerpt = str(cit.get("excerpt") or "").strip()
            sentences = [s for s in _SENT.split(excerpt) if s.strip()] or ([name] if name else [])
            for i, window in enumerate(_windows(sentences, chunk_words, overlap)):
                add("source", f"{name}: {window}" if name and (i or excerpt) else window, [number], name)
    return out
