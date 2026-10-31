"""Glue: index reports and answer hybrid queries for the follow-up chat and the Knowledge Base."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from deep_research.memory.embeddings import Embedder
from deep_research.rag.chunking import Chunk, chunk_report, report_hash
from deep_research.rag.config import RagSettings
from deep_research.rag.embedders import build_embedder, embeddings_ready
from deep_research.rag.hybrid import bm25_scores, rank_by, rrf
from deep_research.rag.index import ChunkIndex, open_index

log = logging.getLogger(__name__)
VECTOR_POOL = 50


@dataclass
class Hit:
    chunk: Chunk
    score: float  # fused RRF score
    bm25: float
    cosine: float | None  # None when the chunk came only from BM25
    overlap: float  # share of query terms present in the chunk (0..1)


def _terms(text: str) -> set[str]:
    from deep_research.assistant.followup import terms

    return terms(text)


class RagService:
    def __init__(self, settings: RagSettings, embedder: Embedder, index: ChunkIndex, reason: str = "") -> None:
        self.settings = settings
        self.embedder = embedder
        self.index = index
        self.reason = reason
        self.last_reindexed = False  # did the last index_report call (re)process the report?

    @property
    def label(self) -> str:
        return f"hybrid BM25 + {self.embedder.name} on {self.index.name}"

    def index_report(self, run_id: str, report: dict[str, Any], *, title: str = "", force: bool = False) -> int:
        """(Re)index one report; returns chunks written, 0 when the stored copy is already current."""
        self.last_reindexed = False
        doc = report_hash(report)
        if not force and self.index.doc_hash(run_id) == doc:
            return 0
        chunks = chunk_report(run_id, report, title=title, chunk_words=self.settings.chunk_words,
                              overlap_words=self.settings.overlap_words)
        vectors = self.embedder.embed([c.text for c in chunks]) if chunks else []
        self.index.replace_run(run_id, chunks, vectors, doc_hash=doc)  # hash stored even for 0 chunks
        self.last_reindexed = True
        return len(chunks)

    def search(self, query: str, k: int | None = None, run_id: str | None = None) -> list[Hit]:
        k = k or self.settings.top_k
        q = _terms(query)
        if not query.strip():
            return []
        pool = self.index.chunks(run_id)
        if not pool:
            return []
        by_id = {c.chunk_id: c for c in pool}
        ids = list(by_id)
        bm = bm25_scores(q, [sorted(_terms(by_id[i].text)) for i in ids])
        bm_of = dict(zip(ids, bm))
        vec_hits = self.index.vector_search(self.embedder.embed([query])[0], VECTOR_POOL, run_id)
        cos_of = {c.chunk_id: s for c, s in vec_hits if c.chunk_id in by_id}
        fused = rrf([rank_by(ids, bm), [c.chunk_id for c, _ in vec_hits if c.chunk_id in by_id]])
        ranked = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))[:k]
        return [Hit(by_id[cid], score, bm_of.get(cid, 0.0), cos_of.get(cid),
                    len(q & _terms(by_id[cid].text)) / len(q) if q else 0.0) for cid, score in ranked]

    def supported(self, hit: Hit, min_overlap: float) -> bool:
        """Evidence gate for answering: semantically close or lexically matching, and cited."""
        close = hit.cosine is not None and hit.cosine >= self.settings.min_similarity
        return bool(hit.chunk.citations) and (close or hit.overlap >= min_overlap)


def build_service(settings: RagSettings | None = None, *, require_enabled: bool = True) -> RagService | None:
    """The configured service, or ``None`` (disabled / embeddings unavailable -> word-overlap fallback)."""
    settings = settings or RagSettings()
    if require_enabled and not settings.enabled:
        return None
    embedder = build_embedder(settings)
    if embedder is None:
        return None
    index, reason = open_index(settings, embedder.name)
    return RagService(settings, embedder, index, reason)


def status(settings: RagSettings | None = None) -> dict[str, Any]:
    """What RAG would use here, without loading a model or touching the network."""
    settings = settings or RagSettings()
    ok, detail = embeddings_ready(settings)
    return {"enabled": settings.enabled, "embeddings_ready": ok, "embeddings": detail, "index": settings.index,
            "retrieval": ("hybrid BM25 + vector (RRF)" if settings.enabled and ok else "word overlap (fallback)")}
