import sys
import types

from deep_research.memory.embeddings import (
    HashingEmbedder,
    SentenceTransformerEmbedder,
    cosine,
    get_embedder,
)


def test_hashing_embedder_is_deterministic_and_normalised() -> None:
    emb = HashingEmbedder(64)
    a, b = emb.embed(["solar panel efficiency", "solar panel efficiency"])
    assert a == b and len(a) == 64
    assert abs(sum(v * v for v in a) - 1.0) < 1e-9


def test_hashing_embedder_similarity_orders_related_text() -> None:
    emb = HashingEmbedder()
    q, near, far = emb.embed(["battery storage costs", "costs of battery storage fell", "medieval poetry"])
    assert cosine(q, near) > cosine(q, far)


def test_empty_text_gives_zero_vector() -> None:
    assert HashingEmbedder(16).embed([""]) == [[0.0] * 16]


class _FakeModel:
    def get_sentence_embedding_dimension(self) -> int:
        return 3

    def encode(self, texts: list[str], normalize_embeddings: bool) -> list[list[float]]:
        assert normalize_embeddings
        return [[1.0, 0.0, 0.0] for _ in texts]


def test_sentence_transformer_embedder_uses_injected_model() -> None:
    emb = SentenceTransformerEmbedder("fake", model=_FakeModel())
    assert emb.dimension == 3 and emb.embed(["x"]) == [[1.0, 0.0, 0.0]]
    assert emb.embed([]) == []


def test_get_embedder_falls_back_without_extra(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    assert isinstance(get_embedder(), HashingEmbedder)
    assert isinstance(get_embedder(prefer_local_model=False), HashingEmbedder)


def test_get_embedder_uses_local_model_when_installed(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    mod = types.ModuleType("sentence_transformers")
    mod.SentenceTransformer = lambda name: _FakeModel()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", mod)
    emb = get_embedder()
    assert isinstance(emb, SentenceTransformerEmbedder) and emb.dimension == 3


def test_cosine_rejects_length_mismatch() -> None:
    import pytest

    with pytest.raises(ValueError, match="mismatch"):
        cosine([1.0, 0.0], [1.0])
