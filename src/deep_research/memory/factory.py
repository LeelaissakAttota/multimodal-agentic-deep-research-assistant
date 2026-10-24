"""Build the configured long-term memory (semantic when the ``memory`` extra is present)."""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from pydantic import ValidationError

from deep_research.evidence.evidence import Evidence
from deep_research.evidence.source import Source

from deep_research.memory.research_memory import BoundedResearchMemory, SemanticResearchMemory
from deep_research.persistence.contracts import ResearchSessionRepository, ResearchSessionSnapshot

MEMORY_DB_NAME = "research_memory.sqlite"


def semantic_extra_available() -> bool:
    """True when chromadb or sentence-transformers is importable (no model is loaded)."""

    return any(importlib.util.find_spec(name) is not None for name in ("chromadb", "sentence_transformers"))


def resolve_memory_mode(mode: str) -> str:
    """Map ``auto`` to ``semantic`` or ``lexical``; pass other modes through."""

    if mode == "auto":
        return "semantic" if semantic_extra_available() else "lexical"
    return mode


MAX_REPORT_PARAGRAPHS = 40


def materialize_report_evidence(snapshot: ResearchSessionSnapshot) -> ResearchSessionSnapshot:
    """Add report-derived sources/evidence (claims, citations, report paragraphs) to a snapshot.

    The orchestrator persists only evidence IDs; without objects long-term memory has nothing to
    retrieve. IDs are uuid5-derived so repeated checkpoints never duplicate items.
    """

    report: dict[str, Any] = snapshot.report_metadata or {}
    if not report:
        return snapshot
    ns = snapshot.state.id
    sources = {s.id: s for s in snapshot.sources}
    evidence = {e.id: e for e in snapshot.evidence}
    title = str(report.get("title") or snapshot.request.objective)[:300]
    report_src = Source(id=uuid5(ns, "report"), title=title, description=snapshot.request.objective[:500],
                        metadata={"origin": "report"})
    sources.setdefault(report_src.id, report_src)

    def add(key: str, content: str, source_id: UUID, kind: str) -> None:
        content = content.strip()
        if content:
            ev = Evidence(id=uuid5(ns, key), source_id=source_id, content=content[:4000], metadata={"origin": kind})
            evidence.setdefault(ev.id, ev)

    for claim in report.get("claims") or []:
        if isinstance(claim, dict):
            add(f"claim:{claim.get('text')}", str(claim.get("text") or ""), report_src.id, "report_claim")
    paragraphs = [p for p in str(report.get("content") or "").split("\n\n") if p.strip()]
    for i, para in enumerate(paragraphs[:MAX_REPORT_PARAGRAPHS]):
        add(f"para:{i}:{para[:200]}", para, report_src.id, "report_paragraph")
    for cite in report.get("citations") or []:
        if not isinstance(cite, dict):
            continue
        ctitle = str(cite.get("title") or "")
        try:
            src = Source(id=uuid5(ns, f"cite:{cite.get('url') or ctitle}"), url=cite.get("url") or None,
                         title=ctitle or None, metadata={"origin": "citation"})
        except ValidationError:
            src = Source(id=uuid5(ns, f"cite:{ctitle}"), title=ctitle or None, metadata={"origin": "citation"})
        sources.setdefault(src.id, src)
        add(f"citeev:{src.id}", ctitle, src.id, "citation")
    return snapshot.model_copy(update={"sources": list(sources.values()), "evidence": list(evidence.values())})


class IndexingSessionRepository:
    """Repository wrapper: materializes report evidence and (semantic mode) indexes each save."""

    def __init__(self, inner: ResearchSessionRepository, memory: SemanticResearchMemory | None = None) -> None:
        self.inner = inner
        self.memory = memory

    def initialize(self) -> None:
        self.inner.initialize()

    def save_session(self, snapshot: ResearchSessionSnapshot) -> None:
        snapshot = materialize_report_evidence(snapshot)
        self.inner.save_session(snapshot)
        if self.memory is not None:
            self.memory.index(snapshot)

    def load_session(self, session_id: UUID) -> ResearchSessionSnapshot:
        return self.inner.load_session(session_id)

    def list_sessions(self, limit: int = 100) -> list[ResearchSessionSnapshot]:
        return self.inner.list_sessions(limit)

    def delete_session(self, session_id: UUID) -> bool:
        return self.inner.delete_session(session_id)


@dataclass
class LongTermMemory:
    mode: str
    repository: ResearchSessionRepository
    memory: BoundedResearchMemory


def build_long_term_memory(mode: str, memory_dir: str | Path) -> LongTermMemory | None:
    """Repository + memory for the orchestrator, or ``None`` when memory is off."""

    resolved = resolve_memory_mode(mode)
    if resolved == "off":
        return None
    from deep_research.persistence.sqlite_repository import SQLiteResearchRepository

    base = Path(memory_dir)
    repo = SQLiteResearchRepository(base / MEMORY_DB_NAME)
    repo.initialize()
    if resolved == "lexical":
        return LongTermMemory("lexical", IndexingSessionRepository(repo), BoundedResearchMemory(repo))
    from deep_research.memory.embeddings import get_embedder
    from deep_research.memory.reranker import get_reranker
    from deep_research.memory.vector_store import get_vector_store

    memory = SemanticResearchMemory(repo, embedder=get_embedder(), store=get_vector_store(base / "vector_store"),
                                    reranker=get_reranker())
    return LongTermMemory("semantic", IndexingSessionRepository(repo, memory), memory)
