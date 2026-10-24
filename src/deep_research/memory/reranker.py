"""Rerank retrieved candidates: free local cross-encoder when installed, lexical overlap otherwise."""

from __future__ import annotations

import re
from typing import Any, Protocol, runtime_checkable

DEFAULT_RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
_TOKEN = re.compile(r"[\w]+", re.UNICODE)
_STOP = frozenset({"a", "an", "and", "for", "in", "is", "of", "on", "or", "the", "to", "with"})


@runtime_checkable
class Reranker(Protocol):
    name: str

    def score(self, query: str, texts: list[str]) -> list[float]:
        """Relevance score per text (higher is better)."""


def _terms(text: str) -> set[str]:
    return {w for w in (t.casefold() for t in _TOKEN.findall(text)) if w not in _STOP and len(w) > 1}


class LexicalReranker:
    """Deterministic fallback: fraction of query terms present in each text (0..1)."""

    name = "lexical"

    def score(self, query: str, texts: list[str]) -> list[float]:
        q = _terms(query)
        if not q:
            return [0.0 for _ in texts]
        return [len(q & _terms(t)) / len(q) for t in texts]


class CrossEncoderReranker:
    """Free local cross-encoder via sentence-transformers (optional ``memory`` extra)."""

    def __init__(self, model_name: str = DEFAULT_RERANK_MODEL, model: Any | None = None) -> None:
        if model is None:
            from sentence_transformers import CrossEncoder

            model = CrossEncoder(model_name)
        self._model = model
        self.name = model_name

    def score(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        return [float(s) for s in self._model.predict([(query, t) for t in texts])]


def rerank(reranker: Reranker, query: str, items: list[tuple[str, str]], top_k: int | None = None) -> list[tuple[str, float]]:
    """Order ``(id, text)`` items by reranker score; ties keep input order."""

    scores = reranker.score(query, [t for _, t in items])
    order = sorted(range(len(items)), key=lambda i: (-scores[i], i))
    ranked = [(items[i][0], scores[i]) for i in order]
    return ranked if top_k is None else ranked[:top_k]


def get_reranker(prefer_local_model: bool = True, model_name: str = DEFAULT_RERANK_MODEL) -> Reranker:
    if prefer_local_model:
        try:
            return CrossEncoderReranker(model_name)
        except Exception:  # noqa: BLE001 - extra missing / weights unavailable offline
            pass
    return LexicalReranker()
