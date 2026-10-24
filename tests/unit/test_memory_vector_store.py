import sys
import types
from pathlib import Path
from typing import Any

from deep_research.memory.vector_store import ChromaVectorStore, LocalVectorStore, get_vector_store


def test_local_store_search_and_persistence(tmp_path: Path) -> None:
    s = LocalVectorStore(tmp_path, "c")
    s.upsert(["a", "b"], [[1.0, 0.0], [0.0, 1.0]], ["ta", "tb"], [{"k": 1}, {"k": 2}])
    hits = s.search([0.9, 0.1], 1)
    assert [h.id for h in hits] == ["a"] and hits[0].metadata == {"k": 1}
    s.upsert(["a"], [[0.0, 1.0]], ["ta2"], [{}])
    reloaded = LocalVectorStore(tmp_path, "c")
    assert reloaded.count() == 2 and reloaded.search([0.0, 1.0], 2)[0].score > 0.99


def test_local_store_in_memory_and_corrupt_file(tmp_path: Path) -> None:
    assert LocalVectorStore().search([1.0], 3) == []
    (tmp_path / "c.json").write_text("{bad", encoding="utf-8")
    assert LocalVectorStore(tmp_path, "c").count() == 0


class _FakeCollection:
    def __init__(self) -> None:
        self.rows: dict[str, Any] = {}

    def upsert(self, ids: list[str], embeddings: Any, documents: Any, metadatas: Any) -> None:
        for i, e, d, m in zip(ids, embeddings, documents, metadatas):
            self.rows[i] = (e, d, m)

    def count(self) -> int:
        return len(self.rows)

    def query(self, query_embeddings: Any, n_results: int) -> dict[str, Any]:
        ids = sorted(self.rows)[:n_results]
        return {"ids": [ids], "distances": [[0.25] * len(ids)], "documents": [[self.rows[i][1] for i in ids]],
                "metadatas": [[self.rows[i][2] for i in ids]]}


class _FakeClient:
    def __init__(self, path: str = "") -> None:
        self.col = _FakeCollection()

    def get_or_create_collection(self, name: str, metadata: Any) -> _FakeCollection:
        assert metadata == {"hnsw:space": "cosine"}
        return self.col


def test_chroma_store_with_fake_client() -> None:
    s = ChromaVectorStore(client=_FakeClient())
    assert s.search([1.0], 3) == []
    s.upsert(["x"], [[1.0]], ["doc"], [{"a": "b"}])
    s.upsert([], [], [], [])
    hit = s.search([1.0], 3)[0]
    assert (hit.id, hit.score, hit.text, hit.metadata) == ("x", 0.75, "doc", {"a": "b"}) and s.count() == 1


def test_factory_fallback_and_chroma(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setitem(sys.modules, "chromadb", None)
    assert isinstance(get_vector_store(tmp_path), LocalVectorStore)
    assert isinstance(get_vector_store(None), LocalVectorStore)
    mod = types.ModuleType("chromadb")
    mod.PersistentClient = _FakeClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "chromadb", mod)
    assert get_vector_store(tmp_path / "vs").backend == "chroma"
