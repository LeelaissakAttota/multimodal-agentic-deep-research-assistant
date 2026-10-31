from deep_research.context.context_builder import (
    ContextPolicy,
    ResearchContextBuilder,
    compress_text,
    estimate_tokens,
    sliding_window,
)
from deep_research.memory.research_memory import SemanticResearchMemory
from tests.unit.test_memory_long_term import _Repo
from tests.unit.test_research_intelligence import make_snapshot


def test_helpers() -> None:
    assert estimate_tokens("") == 0 and estimate_tokens("abcde") == 2
    assert sliding_window([1, 2, 3, 4], 2) == [3, 4]
    assert sliding_window([1, 2, 3, 4], 2, stride_from_end=1) == [2, 3]
    assert sliding_window([1, 2], 0) == [] and sliding_window([1], 5) == [1]


def test_compress_keeps_relevant_sentences_in_order() -> None:
    text = "Cats sleep a lot. Solar storage reliability rose sharply. The weather was mild. Storage costs fell."
    out = compress_text(text, "solar storage reliability", 70)
    assert "Solar storage reliability rose sharply." in out and len(out) <= 70
    assert "weather" not in out
    assert compress_text("short", "q", 100) == "short" and compress_text("x", "q", 0) == ""
    assert len(compress_text("one very long sentence without breaks", "zzz", 10)) == 10


def _builder(policy: ContextPolicy) -> tuple[ResearchContextBuilder, object]:
    snap = make_snapshot()
    mem = SemanticResearchMemory(_Repo())
    mem.store(snap)
    return ResearchContextBuilder(mem, policy), snap


def test_token_budget_caps_text() -> None:
    builder, snap = _builder(ContextPolicy(max_characters=4000, max_tokens=30))
    ctx = builder.build(snap.request, snap.state, "reliability")  # type: ignore[attr-defined]
    assert ctx.text_character_count <= 120 and ctx.estimated_tokens <= 30 and ctx.token_budget == 30
    assert ContextPolicy(max_characters=200, max_tokens=100).character_budget == 200


def test_compression_and_reflection_window() -> None:
    builder, snap = _builder(ContextPolicy(compress_evidence=True, max_evidence_characters=50, reflection_window_iterations=1))
    ctx = builder.build(snap.request, snap.state)  # type: ignore[attr-defined]
    assert ctx.evidence and all(len(e.content_excerpt) <= 50 for e in ctx.evidence)
    assert ctx.compressed_evidence_count >= 1
    assert [r.iteration_number for r in ctx.reflections] == [1]


def test_defaults_unchanged() -> None:
    builder, snap = _builder(ContextPolicy())
    ctx = builder.build(snap.request, snap.state)  # type: ignore[attr-defined]
    assert ctx.compressed_evidence_count == 0 and ctx.token_budget is None and len(ctx.reflections) == 2
