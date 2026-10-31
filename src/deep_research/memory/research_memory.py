"""Bounded, provenance-preserving durable research memory."""

from __future__ import annotations

import math
import re
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, Field

from deep_research.evidence.claim import Claim
from deep_research.evidence.evidence import Evidence
from deep_research.evidence.source import Source
from deep_research.memory.embeddings import Embedder, HashingEmbedder
from deep_research.memory.reranker import LexicalReranker, Reranker
from deep_research.memory.vector_store import LocalVectorStore, VectorStore
from deep_research.persistence.contracts import (
    ResearchSessionRepository,
    ResearchSessionSnapshot,
)

_WORD_PATTERN = re.compile(r"[\w]+", re.UNICODE)
_STOP_WORDS = frozenset(
    {"a", "an", "and", "for", "in", "is", "of", "on", "or", "the", "to", "with"}
)


class MemoryQuery(BaseModel):
    """Deterministic bounded query over durable research evidence."""

    objective: str = ""
    current_task: str | None = None
    session_id: UUID | None = None
    limit: int = Field(default=10, ge=1, le=100)
    max_sessions: int = Field(default=25, ge=1, le=100)


class MemoryRecord(BaseModel):
    """Evidence plus its intact source, claim, task, and session provenance."""

    session_id: UUID
    request_id: UUID
    evidence: Evidence
    source: Source
    claims: list[Claim] = Field(default_factory=list)
    task_ids: list[UUID] = Field(default_factory=list)
    relevance_score: int = Field(default=0, ge=0)


@runtime_checkable
class ResearchMemory(Protocol):
    """Durable memory boundary consumed by context construction."""

    def store(self, snapshot: ResearchSessionSnapshot) -> None:
        """Store a session without mutating historical evidence."""

    def retrieve(self, session_id: UUID) -> ResearchSessionSnapshot:
        """Reconstruct a saved research session."""

    def query(self, query: MemoryQuery) -> list[MemoryRecord]:
        """Return bounded, relevant evidence with provenance."""


def _claims_for(snapshot: ResearchSessionSnapshot, evidence: Evidence) -> list[Claim]:
    return sorted(
        (claim for claim in snapshot.claims if evidence.id in claim.supported_by),
        key=lambda item: str(item.id),
    )


def searchable_text(snapshot: ResearchSessionSnapshot, evidence: Evidence) -> str:
    """Evidence content plus source title/description and supported claim texts."""

    source = next(s for s in snapshot.sources if s.id == evidence.source_id)
    return " ".join(
        [
            evidence.content,
            source.title or "",
            source.description or "",
            *(claim.text for claim in _claims_for(snapshot, evidence)),
        ]
    )


def build_record(snapshot: ResearchSessionSnapshot, evidence: Evidence, score: int) -> MemoryRecord:
    """Wrap one evidence item with its full provenance."""

    source = next(s for s in snapshot.sources if s.id == evidence.source_id)
    return MemoryRecord(
        session_id=snapshot.state.id,
        request_id=snapshot.request.id,
        evidence=evidence,
        source=source,
        claims=_claims_for(snapshot, evidence),
        task_ids=sorted(set(snapshot.evidence_task_links.get(evidence.id, [])), key=str),
        relevance_score=score,
    )


class BoundedResearchMemory:
    """Repository-backed research memory with deterministic lexical retrieval."""

    def __init__(self, repository: ResearchSessionRepository) -> None:
        self.repository = repository

    def store(self, snapshot: ResearchSessionSnapshot) -> None:
        self.repository.save_session(snapshot)

    def retrieve(self, session_id: UUID) -> ResearchSessionSnapshot:
        return self.repository.load_session(session_id).model_copy(deep=True)

    def query(self, query: MemoryQuery) -> list[MemoryRecord]:
        if query.session_id is not None:
            snapshots = [self.repository.load_session(query.session_id)]
        else:
            snapshots = self.repository.list_sessions(limit=query.max_sessions)

        objective_terms = self._terms(query.objective)
        task_terms = self._terms(query.current_task or "")
        has_filter = bool(objective_terms or task_terms)
        records: list[MemoryRecord] = []

        for snapshot in snapshots:
            for evidence in snapshot.evidence:
                searchable_terms = self._terms(searchable_text(snapshot, evidence))
                score = len(objective_terms.intersection(searchable_terms))
                score += 2 * len(task_terms.intersection(searchable_terms))
                if has_filter and score == 0:
                    continue
                records.append(build_record(snapshot, evidence, score))

        records.sort(
            key=lambda item: (
                -item.relevance_score,
                item.evidence.extracted_at.isoformat(),
                str(item.session_id),
                str(item.evidence.id),
            )
        )
        return records[: query.limit]

    @staticmethod
    def _terms(value: str) -> set[str]:
        return {
            word
            for word in (match.group(0).casefold() for match in _WORD_PATTERN.finditer(value))
            if word not in _STOP_WORDS and len(word) > 1
        }


class SemanticResearchMemory(BoundedResearchMemory):
    """Long-term memory: embeddings + vector store + reranker over every stored session.

    Falls back to the deterministic lexical retrieval of :class:`BoundedResearchMemory`
    when the query is empty or nothing relevant is indexed yet. ``relevance_score`` is the
    final relevance scaled to an integer in ``0..1000``.
    """

    def __init__(
        self,
        repository: ResearchSessionRepository,
        embedder: Embedder | None = None,
        store: VectorStore | None = None,
        reranker: Reranker | None = None,
        min_similarity: float = 0.05,
    ) -> None:
        super().__init__(repository)
        self.embedder: Embedder = embedder or HashingEmbedder()
        self.vector_store: VectorStore = store or LocalVectorStore()
        self.reranker: Reranker = reranker or LexicalReranker()
        self.min_similarity = min_similarity

    def store(self, snapshot: ResearchSessionSnapshot) -> None:
        super().store(snapshot)
        self.index(snapshot)

    def index(self, snapshot: ResearchSessionSnapshot) -> int:
        """Embed and upsert every evidence item of a session; returns the count."""

        if not snapshot.evidence:
            return 0
        texts = [searchable_text(snapshot, ev) for ev in snapshot.evidence]
        ids = [f"{snapshot.state.id}:{ev.id}" for ev in snapshot.evidence]
        metas: list[dict[str, str | int | float | bool]] = [
            {"session_id": str(snapshot.state.id), "evidence_id": str(ev.id)} for ev in snapshot.evidence
        ]
        self.vector_store.upsert(ids, self.embedder.embed(texts), texts, metas)
        return len(ids)

    def index_existing(self, max_sessions: int = 100) -> int:
        """Backfill the index from sessions already in the repository."""

        return sum(self.index(s) for s in self.repository.list_sessions(limit=max_sessions))

    def query(self, query: MemoryQuery) -> list[MemoryRecord]:
        text = " ".join(part for part in (query.objective, query.current_task or "") if part).strip()
        if not text or self.vector_store.count() == 0:
            return super().query(query)
        hits = self.vector_store.search(self.embedder.embed([text])[0], query.limit * 4)
        hits = [h for h in hits if h.score >= self.min_similarity]
        if query.session_id is not None:
            hits = [h for h in hits if h.metadata.get("session_id") == str(query.session_id)]
        if not hits:
            return super().query(query)
        rerank_scores = self.reranker.score(text, [h.text for h in hits])
        snapshots: dict[str, ResearchSessionSnapshot | None] = {}
        scored: list[MemoryRecord] = []
        for hit, rr in zip(hits, rerank_scores):
            sid = str(hit.metadata.get("session_id", ""))
            if sid not in snapshots:
                try:
                    snapshots[sid] = self.repository.load_session(UUID(sid))
                except Exception:  # noqa: BLE001 - stale index entry
                    snapshots[sid] = None
            snap = snapshots[sid]
            if snap is None:
                continue
            evidence = next((e for e in snap.evidence if str(e.id) == hit.metadata.get("evidence_id")), None)
            if evidence is None:
                continue
            combined = 0.5 * max(hit.score, 0.0) + 0.5 * _squash(rr)
            scored.append(build_record(snap, evidence, int(round(min(combined, 1.0) * 1000))))
        if not scored:
            return super().query(query)
        scored.sort(key=lambda r: (-r.relevance_score, str(r.session_id), str(r.evidence.id)))
        return scored[: query.limit]


def _squash(value: float) -> float:
    """Map any reranker score to 0..1 (identity for 0..1, logistic otherwise)."""

    if 0.0 <= value <= 1.0:
        return value
    return 1.0 / (1.0 + math.exp(-value))
