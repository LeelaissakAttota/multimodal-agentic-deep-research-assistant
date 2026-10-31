"""Text embeddings: a free local model when installed, a deterministic fallback otherwise.

The optional ``memory`` extra installs ``sentence-transformers`` which runs the free
``sentence-transformers/all-MiniLM-L6-v2`` model locally (no API key, no network after the
first download). Without it, :class:`HashingEmbedder` gives stable, dependency-free vectors
so retrieval still works offline and in tests.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Protocol, runtime_checkable

DEFAULT_LOCAL_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
_TOKEN = re.compile(r"[\w]+", re.UNICODE)


@runtime_checkable
class Embedder(Protocol):
    """Maps texts to fixed-size, L2-normalised float vectors."""

    name: str
    dimension: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts."""


def _normalise(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector] if norm else vector


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity for (possibly un-normalised) vectors; lengths must match."""

    if len(a) != len(b):
        raise ValueError(f"vector length mismatch: {len(a)} != {len(b)}")
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class HashingEmbedder:
    """Deterministic feature-hashing embedder (unigrams + bigrams), no dependencies."""

    def __init__(self, dimension: int = 256) -> None:
        if dimension < 8:
            raise ValueError("dimension must be >= 8")
        self.dimension = dimension
        self.name = f"hashing-{dimension}"

    def _features(self, text: str) -> list[str]:
        words = [w.casefold() for w in _TOKEN.findall(text)]
        return words + [f"{a}_{b}" for a, b in zip(words, words[1:])]

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimension
            for feature in self._features(text):
                digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "big") % self.dimension
                sign = 1.0 if digest[4] & 1 else -1.0
                vector[index] += sign
            out.append(_normalise(vector))
        return out


class SentenceTransformerEmbedder:
    """Free local sentence-transformers model (optional ``memory`` extra)."""

    def __init__(self, model_name: str = DEFAULT_LOCAL_MODEL, model: Any | None = None) -> None:
        if model is None:
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(model_name)
        self._model = model
        self.name = model_name
        self.dimension = int(model.get_sentence_embedding_dimension() or 0)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._model.encode(texts, normalize_embeddings=True)
        return [[float(v) for v in row] for row in vectors]


def get_embedder(prefer_local_model: bool = True, model_name: str = DEFAULT_LOCAL_MODEL) -> Embedder:
    """Return the local model embedder when available, else the deterministic fallback."""

    if prefer_local_model:
        try:
            return SentenceTransformerEmbedder(model_name)
        except Exception:  # noqa: BLE001 - missing extra, no weights offline, etc.
            pass
    return HashingEmbedder()
