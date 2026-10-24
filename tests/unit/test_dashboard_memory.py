from pathlib import Path
from typing import Any

from deep_research.dashboard.core import memory_items, memory_search, memory_status


def _run(rid: str, title: str, claim: str, cite: str) -> dict[str, Any]:
    return {"run_id": rid, "created_at": "2026-10-24T10:00:00Z", "params": {"objective": title},
            "result": {"report": {"title": title, "claims": [{"text": claim, "confidence": 0.8}],
                                  "citations": [{"number": 1, "title": cite, "url": f"https://x.org/{rid}"}]}}}


RUNS = [_run("r1", "Solar storage", "Battery storage reliability improved in field tests.", "Solar field study"),
        _run("r2", "Medieval poetry", "Chaucer wrote in Middle English.", "Poetry anthology"), {"run_id": "r3"}]


def test_memory_items_from_real_reports() -> None:
    items = memory_items(RUNS)
    assert {i["kind"] for i in items} == {"claim", "source"} and len(items) == 4


def test_memory_search_ranks_relevant_first() -> None:
    hits = memory_search(RUNS, "battery storage reliability")
    assert hits[0]["run_id"] == "r1" and hits[0]["kind"] == "claim" and hits[0]["score"] > 0
    assert memory_search(RUNS, "  ") == [] and memory_search([], "x") == []


def test_memory_status_reports_backends(tmp_path: Path) -> None:
    (tmp_path / "research_memory.json").write_text("{}", encoding="utf-8")
    ms = memory_status(tmp_path)
    assert ms["bytes_on_disk"] == 2 and ms["path"] == str(tmp_path)
    assert "fallback" in ms["embedder"] or "local" in ms["embedder"]
    assert memory_status(tmp_path / "missing")["bytes_on_disk"] == 0


def test_search_prefers_persistent_store(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import sys

    from deep_research.memory.embeddings import HashingEmbedder
    from deep_research.memory.vector_store import LocalVectorStore

    for name in ("chromadb", "sentence_transformers"):
        monkeypatch.setitem(sys.modules, name, None)
    runs = [dict(RUNS[0], result={**RUNS[0]["result"], "session_id": "s-1"})]
    store, emb = LocalVectorStore(tmp_path), HashingEmbedder()
    texts = ["Persistent battery storage reliability finding", "unrelated poetry"]
    store.upsert(["s-1:e1", "s-2:e2"], emb.embed(texts), texts, [{"session_id": "s-1"}, {"session_id": "s-2"}])
    hits = memory_search(runs, "battery storage reliability", vector_dir=tmp_path)
    assert hits[0]["backend"] == "persistent" and hits[0]["run_id"] == "r1" and hits[0]["kind"] == "memory"
    empty = tmp_path / "empty"
    empty.mkdir()
    assert memory_search(runs, "battery storage", vector_dir=empty)[0]["backend"] == "in-memory"
    assert memory_search(runs, "battery storage", vector_dir=tmp_path / "nope")[0]["backend"] == "in-memory"


def test_persistent_search_falls_back_on_broken_store(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from deep_research.dashboard import core

    (tmp_path / "x").write_text("x", encoding="utf-8")

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("broken")

    monkeypatch.setattr("deep_research.memory.vector_store.get_vector_store", boom)
    assert core.persistent_memory_search(RUNS, "battery", 5, tmp_path) == []
    assert memory_search(RUNS, "battery storage", vector_dir=tmp_path)[0]["backend"] == "in-memory"
