"""Edge-case tests for Phase 3 memory/context modules (fallback paths only)."""

from __future__ import annotations

import builtins
from pathlib import Path
from typing import Any

import pytest

from deep_research.context.context_builder import compress_text, estimate_tokens, sliding_window
from deep_research.memory import embeddings as emb
from deep_research.memory import reranker as rr
from deep_research.memory import vector_store as vs


def test_hashing_empty_and_unicode() -> None:
    e = emb.HashingEmbedder(16)
    assert e.embed([]) == []
    zero, uni = e.embed(["", "café 東京 Ünïcode"])
    assert zero == [0.0] * 16
    assert abs(sum(v * v for v in uni) - 1.0) < 1e-9
    assert e.embed(["Café"]) == e.embed(["café"])
    assert e.embed(["x " * 50_000])[0] == e.embed(["x " * 50_000])[0]
    with pytest.raises(ValueError):
        emb.HashingEmbedder(7)
    assert emb.cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


def _block_imports(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    real = builtins.__import__

    def fake(name: str, *a: Any, **k: Any) -> Any:
        if name.split(".")[0] in names:
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)


def test_fallbacks_when_optional_deps_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _block_imports(monkeypatch, "sentence_transformers", "chromadb")
    assert isinstance(emb.get_embedder(), emb.HashingEmbedder)
    assert isinstance(rr.get_reranker(), rr.LexicalReranker)
    assert isinstance(vs.get_vector_store(tmp_path), vs.LocalVectorStore)
    assert isinstance(vs.get_vector_store(None), vs.LocalVectorStore)


def test_store_k_larger_than_size_negative_k_duplicates(tmp_path: Path) -> None:
    s = vs.LocalVectorStore(tmp_path)
    assert s.search([1.0, 0.0], 5) == []
    s.upsert(["a", "a", "b"], [[1.0, 0.0], [0.0, 1.0], [1.0, 0.0]], ["t1", "t2", "t3"], [{}, {"k": 1}, {}])
    assert s.count() == 2
    hits = s.search([0.0, 1.0], 100)
    assert [h.id for h in hits] == ["a", "b"] and hits[0].text == "t2"
    assert s.search([1.0, 0.0], -1) == []
    assert vs.LocalVectorStore(tmp_path).count() == 2  # persisted
    with pytest.raises(ValueError):
        vs.LocalVectorStore().upsert(["a"], [], ["t"], [{}])
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "research_memory.json").write_text("{not json", encoding="utf-8")
    c = vs.LocalVectorStore(bad)
    assert c.count() == 0
    c.upsert(["a"], [[1.0]], ["t"], [{}])
    assert vs.LocalVectorStore(bad).count() == 1


@pytest.mark.xfail(strict=True, reason="BUG: valid non-object JSON (e.g. []) is accepted as the item map")
def test_store_non_object_json_file(tmp_path: Path) -> None:
    (tmp_path / "research_memory.json").write_text("[]", encoding="utf-8")
    s = vs.LocalVectorStore(tmp_path)
    s.upsert(["a"], [[1.0]], ["t"], [{}])
    assert s.count() == 1


def test_lexical_reranker_edges() -> None:
    r = rr.LexicalReranker()
    assert r.score("the of a", ["anything"]) == [0.0]
    assert r.score("x", []) == []
    assert r.score("Café data", ["CAFÉ", "nothing"]) == [0.5, 0.0]
    assert rr.rerank(r, "q", []) == []
    items = [("1", "alpha"), ("2", "alpha beta"), ("3", "alpha")]
    assert [i for i, _ in rr.rerank(r, "alpha beta", items)] == ["2", "1", "3"]
    assert rr.rerank(r, "alpha", items, top_k=0) == []


def test_token_estimate_window_and_compress() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcde") == 2
    assert sliding_window([1, 2, 3], 0) == []
    assert sliding_window([1, 2, 3], -2) == []
    assert sliding_window([1, 2, 3], 10) == [1, 2, 3]
    assert sliding_window([1, 2, 3], 2, stride_from_end=1) == [1, 2]
    assert sliding_window([1, 2, 3], 2, stride_from_end=99) == []
    assert sliding_window([], 3) == []
    text = "Alpha is first. Beta matters here! Gamma is last?"
    assert compress_text(text, "beta", 0) == ""
    assert compress_text(text, "beta", -5) == ""
    assert compress_text(text, "beta", 1000) == text
    out = compress_text(text, "beta matters", 20)
    assert out == "Beta matters here!" and len(out) <= 20
    long = "word " * 10_000 + "."
    assert len(compress_text(long, "word", 100)) <= 100
    assert len(compress_text("東京は大きい。" * 100, "東京", 10)) <= 10


def test_compress_text_whitespace_only() -> None:
    assert compress_text("      ", "q", 2) == ""
