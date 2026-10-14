"""Turn processed attachments (PDF pages, image descriptions) into primary synthesis sources.

Attachment tasks run before web tasks. Their output is condensed into small
JSON-safe *entries* stored on the research state. Each later web task gets
the entries, picks the most relevant chunks for its query, and puts them
**before** web sources in the numbered list sent to the LLM, so the model
cites the user's documents first.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from deep_research.evidence.evidence import Evidence
from deep_research.evidence.source import Source

ATTACHMENT_TOOL_KINDS = {"pdf_reader": "pdf", "image_analyzer": "image"}
CHUNK_CHARS = 1_200
MAX_ENTRY_CHARS = 40_000
_WORD = re.compile(r"[a-z0-9]{3,}")
_STOP = frozenset(
    "the and for are with that this from what which who how why when main using about into than then they them "
    "their there these those have has had was were will would can could should does did not but any all its".split()
)


def attachment_entry(tool_id: str | None, output: Mapping[str, Any]) -> dict[str, Any] | None:
    """Condense a successful attachment-tool output; ``None`` for non-attachment tasks."""

    kind = ATTACHMENT_TOOL_KINDS.get(tool_id or "")
    if kind is None:
        return None
    sources = output.get("source_objects")
    first = sources[0] if isinstance(sources, list) and sources else None
    title = str(getattr(first, "title", None) or f"{kind} attachment")
    chunks: list[dict[str, Any]] = []
    budget = MAX_ENTRY_CHARS
    pages = output.get("pages")
    if isinstance(pages, list) and pages:
        for page in pages:
            if not isinstance(page, Mapping):
                continue
            for piece in _split(str(page.get("text") or ""), CHUNK_CHARS):
                if budget <= 0:
                    break
                chunks.append({"page": page.get("page"), "text": piece[:budget]})
                budget -= len(piece)
    else:
        summary = str(output.get("summary") or "").strip()
        if summary:
            chunks.append({"page": None, "text": summary[:MAX_ENTRY_CHARS]})
    if not chunks:
        return None
    return {
        "kind": kind,
        "title": title,
        "page_count": output.get("page_count"),
        "has_text": output.get("has_text", kind == "pdf"),
        "note": output.get("note"),
        "image_count": output.get("image_count"),
        "chunks": chunks,
    }


def select_attachment_sources(
    entries: Any, query: str, *, max_chunks: int = 4, max_chars: int = 1_500
) -> tuple[list[Source], list[Evidence]]:
    """Most query-relevant attachment chunks as (Source, Evidence) pairs, document order kept per file."""

    if not isinstance(entries, list):
        return [], []
    terms = _terms(query)
    scored: list[tuple[float, int, dict[str, Any], dict[str, Any]]] = []
    order = 0
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        for chunk in entry.get("chunks") or []:
            if not isinstance(chunk, Mapping) or not str(chunk.get("text") or "").strip():
                continue
            overlap = len(terms & _terms(str(chunk["text"])))
            # Non-text attachments (image descriptions / scans) are always worth one slot.
            bonus = 0.5 if entry.get("kind") == "image" or entry.get("has_text") is False else 0.0
            scored.append((overlap + bonus, order, dict(entry), dict(chunk)))
            order += 1
    if not scored:
        return [], []
    picked = sorted(scored, key=lambda item: (-item[0], item[1]))[:max_chunks]
    picked.sort(key=lambda item: item[1])
    sources: list[Source] = []
    evidence: list[Evidence] = []
    for _, _, entry, chunk in picked:
        page = chunk.get("page")
        label = f"{entry['title']} (attached {entry['kind']}{f', p.{page}' if page else ''})"
        source = Source(title=label, description=f"User-attached {entry['kind']}",
                        metadata={"attachment": True, "attachment_kind": entry["kind"], "page": page,
                                  "attachment_title": entry["title"]})
        sources.append(source)
        evidence.append(Evidence(source_id=source.id, content=str(chunk["text"])[:max_chars],
                                 content_type=f"attachment_{entry['kind']}",
                                 metadata={"attachment": True, "page": page, "provenance": "attachment"}))
    return sources, evidence


def _split(text: str, size: int) -> list[str]:
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    pieces: list[str] = []
    paragraphs = re.split(r"\n\s*\n", text)
    current = ""
    for para in paragraphs:
        while len(para) > size:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(para[:size])
            para = para[size:]
        if len(current) + len(para) + 2 > size and current:
            pieces.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current.strip():
        pieces.append(current)
    return pieces


def _terms(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}
