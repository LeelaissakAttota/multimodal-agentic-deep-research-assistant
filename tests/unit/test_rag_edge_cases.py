"""Edge cases for phase 7 (A) RAG (Testing Bot)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from deep_research.rag.backfill import backfill
from deep_research.rag.chunking import chunk_report, cite_numbers
from deep_research.rag.config import RagSettings
from deep_research.rag.hybrid import RRF_K, bm25_scores, rrf
from deep_research.rag.index import LocalIndex
from tests.unit.test_rag import REPORT, service


def test_rrf_k_is_60_and_formula() -> None:
    assert RRF_K == 60
    fused = rrf([["a", "b"], ["b"]])
    assert fused["b"] == pytest.approx(1 / 62 + 1 / 61) and fused["a"] == pytest.approx(1 / 61)


def test_bm25_empty_inputs() -> None:
    assert bm25_scores(set(), [["a"]]) == [0.0]
    assert bm25_scores({"a"}, []) == []
    assert bm25_scores({"a"}, [[]]) == [0.0]


def test_disabled_by_default_ignores_unrelated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MDRA_RAG_ENABLED", raising=False)
    assert RagSettings(_env_file=None).enabled is False  # type: ignore[call-arg]


def test_empty_report_yields_no_chunks_and_search_on_empty_index(tmp_path: Path) -> None:
    assert chunk_report("r", {}) == []
    rag = service(tmp_path)
    assert rag.index_report("r", {"content": "", "citations": []}) == 0
    assert rag.search("anything") == [] and rag.search("   ") == []


def test_citations_unknown_and_reversed_ranges() -> None:
    assert cite_numbers("x [9] [2-1] [1\u20133]", {1, 2, 3}) == [1, 2, 3]


def test_duplicate_sentences_get_distinct_chunk_ids() -> None:
    rep = {"content": "Same text here.\n\nSame text here.", "citations": []}
    chunks = chunk_report("r", rep, chunk_words=20, overlap_words=0)
    assert len({c.chunk_id for c in chunks}) == len(chunks)


def test_unicode_report_indexes_and_searches(tmp_path: Path) -> None:
    rep = {"content": "# శీర్షిక\nహీట్ పంపులు చలిలో పనిచేస్తాయి [1]. Café naïve 中文 [1].",
           "citations": [{"number": 1, "title": "మూలం", "excerpt": "వివరణ"}]}
    rag = service(tmp_path)
    assert rag.index_report("u", rep) > 0
    hits = rag.search("Café 中文", run_id="u")
    assert hits and all(1 in h.chunk.citations for h in hits if h.chunk.kind == "report")


def test_huge_doc_with_punctuation_is_windowed() -> None:
    rep = {"content": " ".join(f"Sentence number {i} is here." for i in range(2000)), "citations": []}
    chunks = chunk_report("r", rep, chunk_words=120, overlap_words=30)
    assert len(chunks) > 50 and max(len(c.text.split()) for c in chunks) <= 125


def test_unpunctuated_huge_doc_is_capped() -> None:
    rep = {"content": " ".join(["word"] * 5000), "citations": []}
    chunks = chunk_report("r", rep, chunk_words=120, overlap_words=30)
    assert max(len(c.text.split()) for c in chunks) <= 120


def test_corrupt_jsonl_lines_are_skipped(tmp_path: Path) -> None:
    rag = service(tmp_path)
    rag.index_report("a", REPORT)
    path = rag.index.path  # type: ignore[attr-defined]
    good = path.read_text(encoding="utf-8")
    path.write_text("garbage\n[1,2]\n42\n{\"chunk\": [], \"vector\": [1]}\n{\"chunk\": {}, \"vector\": [1]}\n"
                    + good + "\x00\x00", encoding="utf-8")
    fresh = LocalIndex(path.parent.parent, rag.embedder.name)
    assert fresh.count() == good.count("\n")
    assert fresh.chunks("a")


def test_force_reindex_is_idempotent(tmp_path: Path) -> None:
    rag = service(tmp_path)
    runs: list[dict[str, Any]] = [{"run_id": "a", "result": {"report": REPORT}}]
    backfill(rag, runs)
    ids = [c.chunk_id for c in rag.index.chunks()]
    for _ in range(3):
        assert backfill(rag, runs, force=True).indexed == 1
    assert [c.chunk_id for c in rag.index.chunks()] == ids
    path = rag.index.path  # type: ignore[attr-defined]
    assert len(path.read_text(encoding="utf-8").splitlines()) == len(ids)


def test_changed_report_replaces_old_chunks(tmp_path: Path) -> None:
    rag = service(tmp_path)
    rag.index_report("a", REPORT)
    changed = json.loads(json.dumps(REPORT))
    changed["content"] = "# New\nTotally different text [1]."
    assert rag.index_report("a", changed) > 0
    assert all("Totally" in c.text or c.kind != "report" for c in rag.index.chunks("a"))
