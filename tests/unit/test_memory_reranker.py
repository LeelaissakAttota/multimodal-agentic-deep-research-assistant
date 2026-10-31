import sys
import types

from deep_research.memory.reranker import CrossEncoderReranker, LexicalReranker, get_reranker, rerank


def test_lexical_reranker_orders_by_overlap() -> None:
    r = LexicalReranker()
    assert r.score("the", ["x"]) == [0.0]
    ranked = rerank(r, "solar battery cost", [("a", "poetry"), ("b", "battery cost"), ("c", "solar battery cost")])
    assert [i for i, _ in ranked] == ["c", "b", "a"]
    assert rerank(r, "solar", [("a", "x"), ("b", "y")], top_k=1) == [("a", 0.0)]


class _FakeCE:
    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        return [float(len(t)) for _, t in pairs]


def test_cross_encoder_with_fake_model() -> None:
    r = CrossEncoderReranker("fake", model=_FakeCE())
    assert r.score("q", ["aa", "a"]) == [2.0, 1.0] and r.score("q", []) == []


def test_factory(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    assert isinstance(get_reranker(), LexicalReranker)
    mod = types.ModuleType("sentence_transformers")
    mod.CrossEncoder = lambda name: _FakeCE()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", mod)
    assert isinstance(get_reranker(), CrossEncoderReranker)
    assert isinstance(get_reranker(prefer_local_model=False), LexicalReranker)
