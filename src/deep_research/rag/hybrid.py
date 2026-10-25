"""Hybrid retrieval: Okapi BM25 over chunk text + vector similarity, fused by reciprocal rank (RRF).

RRF score of a chunk = sum over the two rankings of ``1 / (RRF_K + rank)`` (rank starts at 1, a
chunk missing from a ranking contributes nothing). RRF needs no score calibration between BM25
and cosine, and a chunk ranked well by both wins.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Hashable, Sequence
from typing import TypeVar

RRF_K = 60
K1 = 1.5
B = 0.75
T = TypeVar("T", bound=Hashable)


def bm25_scores(query: set[str], docs: Sequence[Sequence[str]]) -> list[float]:
    """Okapi BM25 score of every document (a token list) for the query terms."""
    if not docs or not query:
        return [0.0 for _ in docs]
    n = len(docs)
    avg = sum(len(d) for d in docs) / n or 1.0
    df: Counter[str] = Counter()
    for d in docs:
        df.update(set(d) & query)
    out = []
    for d in docs:
        tf = Counter(t for t in d if t in query)
        score = 0.0
        for term, f in tf.items():
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * f * (K1 + 1) / (f + K1 * (1 - B + B * len(d) / avg))
        out.append(score)
    return out


def rrf(rankings: Sequence[Sequence[T]], k: int = RRF_K) -> dict[T, float]:
    fused: dict[T, float] = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking, start=1):
            fused[key] = fused.get(key, 0.0) + 1.0 / (k + rank)
    return fused


def rank_by(keys: Sequence[T], scores: Sequence[float], *, positive_only: bool = True) -> list[T]:
    order = sorted(range(len(keys)), key=lambda i: (-scores[i], i))
    return [keys[i] for i in order if not positive_only or scores[i] > 0]

