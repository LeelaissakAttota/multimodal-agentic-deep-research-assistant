"""Local vector store: Chroma when the ``memory`` extra is installed, a JSON-backed fallback otherwise.

Data lives under ``data/vector_store`` by default (git-ignored). Nothing leaves the machine.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from deep_research.memory.embeddings import cosine

logger = logging.getLogger(__name__)

DEFAULT_VECTOR_DIR = Path("data") / "vector_store"
DEFAULT_COLLECTION = "research_memory"

Metadata = dict[str, str | int | float | bool]


@dataclass
class VectorHit:
    id: str
    score: float
    text: str
    metadata: Metadata = field(default_factory=dict)


@runtime_checkable
class VectorStore(Protocol):
    backend: str

    def upsert(self, ids: list[str], vectors: list[list[float]], texts: list[str], metadatas: list[Metadata]) -> None:
        """Insert or replace items."""

    def search(self, vector: list[float], k: int) -> list[VectorHit]:
        """Top-k items by cosine similarity (score in [-1, 1])."""

    def count(self) -> int:
        """Number of stored items."""


class LocalVectorStore:
    """Dependency-free cosine store; persists to ``<dir>/<collection>.json`` when a dir is given."""

    backend = "local-json"

    def __init__(self, directory: Path | None = None, collection: str = DEFAULT_COLLECTION) -> None:
        self.path = directory / f"{collection}.json" if directory is not None else None
        self._items: dict[str, dict[str, Any]] = {}
        if self.path is not None and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
                logger.warning("vector store file %s unreadable; starting empty", self.path)
            if not isinstance(data, dict):
                logger.warning("vector store file %s is not a JSON object; starting empty", self.path)
                data = {}
            self._items = data

    def upsert(self, ids: list[str], vectors: list[list[float]], texts: list[str], metadatas: list[Metadata]) -> None:
        for i, v, t, m in zip(ids, vectors, texts, metadatas, strict=True):
            self._items[i] = {"vector": v, "text": t, "metadata": dict(m)}
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._items), encoding="utf-8")

    def search(self, vector: list[float], k: int) -> list[VectorHit]:
        hits = [VectorHit(i, cosine(vector, it["vector"]), it["text"], it["metadata"])
                for i, it in self._items.items() if len(it["vector"]) == len(vector)]
        if len(hits) < len(self._items):
            logger.warning("skipped %d stored vectors with a different dimension", len(self._items) - len(hits))
        hits.sort(key=lambda h: (-h.score, h.id))
        return hits[: max(k, 0)]

    def count(self) -> int:
        return len(self._items)


class ChromaVectorStore:
    """Persistent local Chroma collection (cosine space)."""

    backend = "chroma"

    def __init__(self, directory: Path = DEFAULT_VECTOR_DIR, collection: str = DEFAULT_COLLECTION,
                 client: Any | None = None) -> None:
        if client is None:
            import chromadb

            directory.mkdir(parents=True, exist_ok=True)
            client = chromadb.PersistentClient(path=str(directory))
        self._collection = client.get_or_create_collection(name=collection, metadata={"hnsw:space": "cosine"})

    def upsert(self, ids: list[str], vectors: list[list[float]], texts: list[str], metadatas: list[Metadata]) -> None:
        if ids:
            self._collection.upsert(ids=ids, embeddings=vectors, documents=texts, metadatas=metadatas)

    def search(self, vector: list[float], k: int) -> list[VectorHit]:
        n = min(k, self.count())
        if n <= 0:
            return []
        res = self._collection.query(query_embeddings=[vector], n_results=n)
        ids, dists = res["ids"][0], res["distances"][0]
        docs = (res.get("documents") or [[None] * len(ids)])[0]
        metas = (res.get("metadatas") or [[None] * len(ids)])[0]
        return [VectorHit(str(i), 1.0 - float(d), str(t or ""), dict(m or {})) for i, d, t, m in zip(ids, dists, docs, metas)]

    def count(self) -> int:
        return int(self._collection.count())


def get_vector_store(directory: Path | None = DEFAULT_VECTOR_DIR, collection: str = DEFAULT_COLLECTION,
                     prefer_chroma: bool = True) -> VectorStore:
    """Chroma when installed and a directory is given; otherwise the local fallback."""

    if prefer_chroma and directory is not None:
        try:
            return ChromaVectorStore(directory, collection)
        except Exception:  # noqa: BLE001 - extra not installed or store unusable
            pass
    return LocalVectorStore(directory, collection)
