"""Hybrid RAG for report follow-up chat and the Knowledge Base (network mocked, local index on tmp_path)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from deep_research.assistant.followup import Answer, answer_question, ask_report, extractive_answer
from deep_research.memory.embeddings import HashingEmbedder
from deep_research.rag.backfill import backfill, saved_runs
from deep_research.rag.chunking import chunk_report, cite_numbers, report_hash
from deep_research.rag.config import RagSettings
from deep_research.rag.embedders import (
    EmbeddingUnavailable,
    LocalSTEmbedder,
    OpenAICompatEmbedder,
    build_embedder,
    embeddings_ready,
    free_api_model,
)
from deep_research.rag.hybrid import bm25_scores, rank_by, rrf
from deep_research.rag.index import LocalIndex, PgVectorIndex, _vec_literal, model_slug, open_index
from deep_research.rag.service import RagService, build_service, status

REPORT: dict[str, Any] = {
    "content": (
        "# Heat pumps in cold climates\n\n## Efficiency\n"
        "Modern cold-climate heat pumps keep a coefficient of performance above 2 at -15 C [1]. "
        "Field trials in Maine showed lower winter heating bills than oil furnaces [2].\n\n"
        "## Costs\nInstallation costs between 12,000 and 20,000 dollars for a typical home [3]. "
        "Federal tax credits cover thirty percent of the installed cost [3].\n\n"
        "## Sources\n1. NEEP cold climate list\n"
    ),
    "citations": [
        {"number": 1, "title": "NEEP cold climate air source heat pump list", "url": "https://neep.org/ccashp",
         "excerpt": "Listed units report COP at 5 F and -15 C. Most models exceed COP 1.75 at the lowest test point."},
        {"number": 2, "title": "Maine heat pump field study", "url": "https://example.org/maine",
         "excerpt": "Households replacing oil saw annual savings of several hundred dollars."},
        {"number": 3, "title": "Energy Star tax credit guide", "url": "https://energystar.gov/credits",
         "excerpt": "The credit is 30 percent of project cost up to 2,000 dollars per year."},
    ],
    "claim_scores": [{"text": "Cold-climate heat pumps stay efficient at -15 C", "citations": [1], "confidence": 0.8}],
}


def settings(tmp_path: Path, **kw: Any) -> RagSettings:
    values: dict[str, Any] = {"enabled": True, "index": "local", "index_dir": str(tmp_path / "rag"), **kw}
    return RagSettings(_env_file=None, **values)  # type: ignore[call-arg]


def service(tmp_path: Path, **kw: Any) -> RagService:
    st = settings(tmp_path, **kw)
    emb = HashingEmbedder(256)
    return RagService(st, emb, LocalIndex(Path(st.index_dir), emb.name), "test")


# ----- chunking ---------------------------------------------------------------------------------
def test_chunks_cover_body_claims_and_sources_with_valid_citations() -> None:
    chunks = chunk_report("r1", REPORT, title="Heat pumps", chunk_words=40, overlap_words=8)
    kinds = [c.kind for c in chunks]
    assert {"report", "claim", "source"} == set(kinds)
    body = [c for c in chunks if c.kind == "report"]
    assert {c.section for c in body} == {"Efficiency", "Costs"}  # split at headings; Sources list excluded
    assert all("NEEP cold climate list" not in c.text for c in body)
    assert any(c.citations == [1, 2] for c in body)
    assert [c.citations for c in chunks if c.kind == "source"] == [[1], [2], [3]]
    assert all(c.title == "Heat pumps" and c.doc_hash == report_hash(REPORT) for c in chunks)
    assert len({c.chunk_id for c in chunks}) == len(chunks)


def test_window_overlap_carries_trailing_sentence() -> None:
    sents = " ".join(f"Sentence number {i} talks about topic {i} here [1]." for i in range(12))
    rep = {"content": f"## A\n{sents}", "citations": [{"number": 1, "title": "t"}]}
    body = [c for c in chunk_report("r", rep, chunk_words=24, overlap_words=10) if c.kind == "report"]
    assert len(body) > 2
    for a, b in zip(body, body[1:]):
        assert b.text.split(". ")[0] in a.text  # first sentence of the next window repeats the last one
    assert all(len(c.text.split()) <= 24 + 10 for c in body)


def test_citation_numbers_are_validated_and_ranges_expand() -> None:
    assert cite_numbers("see [1-3] and [9] and [2, 4]", {1, 2, 3, 4}) == [1, 2, 3, 4]
    assert report_hash(REPORT) != report_hash({**REPORT, "content": "changed"})


# ----- BM25 + RRF --------------------------------------------------------------------------------
def test_bm25_prefers_rare_terms_and_rrf_rewards_agreement() -> None:
    docs = [["heat", "pump", "cost"], ["heat", "oil"], ["tax", "credit", "cost"]]
    scores = bm25_scores({"tax", "cost"}, docs)
    assert scores[2] > scores[0] > scores[1] == 0.0
    assert rank_by(["a", "b", "c"], scores) == ["c", "a"]
    fused = rrf([["x", "y", "z"], ["y", "x"]])
    assert fused["x"] == fused["y"] > fused["z"]
    assert fused["z"] == pytest.approx(1 / 63)


# ----- local index ------------------------------------------------------------------------------
def test_local_index_replaces_runs_and_persists(tmp_path: Path) -> None:
    rag = service(tmp_path)
    assert rag.index_report("r1", REPORT, title="Heat pumps") > 0
    assert rag.index_report("r1", REPORT) == 0  # unchanged report -> no re-embedding
    first = rag.index.count()
    changed = {**REPORT, "content": "## Only\nA single new sentence about geothermal loops [1]."}
    assert rag.index_report("r1", changed) > 0
    assert rag.index.count() < first  # old chunks of r1 replaced, not appended
    reopened = LocalIndex(tmp_path / "rag", "hashing-256")
    assert reopened.doc_hash("r1") == report_hash(changed) and reopened.count() == rag.index.count()
    assert (tmp_path / "rag" / model_slug("hashing-256") / "chunks.jsonl").is_file()


def test_local_index_skips_torn_lines_and_other_dimensions(tmp_path: Path) -> None:
    idx = LocalIndex(tmp_path, "m")
    idx.path.parent.mkdir(parents=True)
    idx.path.write_text("{not json\n", encoding="utf-8")
    assert idx.count() == 0
    chunk = chunk_report("r", REPORT)[0]
    idx.replace_run("r", [chunk], [[1.0, 0.0]])
    assert idx.vector_search([1.0, 0.0, 0.0], 3) == []
    assert idx.vector_search([1.0, 0.0], 3)[0][1] == pytest.approx(1.0)
    with pytest.raises(ValueError):
        idx.replace_run("r", [chunk], [])


def test_hybrid_search_is_scoped_to_the_run_and_ranks_relevant_chunks(tmp_path: Path) -> None:
    rag = service(tmp_path)
    rag.index_report("r1", REPORT)
    rag.index_report("r2", {"content": "## X\nTax credits for electric cars are separate [1].",
                            "citations": [{"number": 1, "title": "IRS EV credit"}]})
    hits = rag.search("How much do tax credits cover?", run_id="r1")
    assert hits and all(h.chunk.run_id == "r1" for h in hits)
    assert 3 in hits[0].chunk.citations and hits[0].bm25 > 0 and hits[0].cosine is not None
    assert {h.chunk.run_id for h in rag.search("tax credits")} == {"r1", "r2"}
    assert rag.search("   ") == []


# ----- follow-up chat ----------------------------------------------------------------------------
def test_chat_uses_hybrid_retrieval_with_citations(tmp_path: Path) -> None:
    rag = service(tmp_path)
    ans = extractive_answer("What share of the cost do tax credits cover?", REPORT, rag, "r1")
    assert ans.supported and ans.retrieval == "hybrid" and 3 in ans.citations
    assert "[3]" in ans.text and ans.as_meta()["retrieval"] == "hybrid"
    assert rag.index.doc_hash("r1") == report_hash(REPORT)  # indexed on first question


def test_chat_refuses_off_topic_questions_with_hybrid(tmp_path: Path) -> None:
    ans = extractive_answer("Who won the football world cup?", REPORT, service(tmp_path), "r1")
    assert not ans.supported and ans.mode == "refused" and ans.retrieval == "hybrid"


def test_chat_without_rag_keeps_word_overlap() -> None:
    ans = extractive_answer("What share of the cost do tax credits cover?", REPORT)
    assert ans.retrieval == "overlap" and ans.supported


def test_rag_failure_falls_back_to_overlap(tmp_path: Path) -> None:
    rag = service(tmp_path)

    def boom(*_: Any, **__: Any) -> int:
        raise OSError("disk full")

    rag.index_report = boom  # type: ignore[method-assign]
    ans = extractive_answer("What share of the cost do tax credits cover?", REPORT, rag, "r1")
    assert ans.retrieval == "overlap" and ans.supported and ans.reason == "rag fallback: OSError"


def test_llm_answer_citations_must_come_from_hybrid_passages(tmp_path: Path) -> None:
    class Gateway:
        def __init__(self, reply: dict[str, Any]) -> None:
            self.reply = reply

    async def run(reply: dict[str, Any]) -> Answer:
        import deep_research.assistant.followup as fu

        async def fake_ask(_gw: Any, _prompt: str, **_: Any) -> tuple[dict[str, Any], None]:
            return reply, None

        orig = fu.ask_json
        fu.ask_json = fake_ask  # type: ignore[assignment]
        try:
            return await answer_question("What do tax credits cover?", REPORT, Gateway(reply),  # type: ignore[arg-type]
                                         rag=service(tmp_path), run_id="r1")
        finally:
            fu.ask_json = orig

    good = asyncio.run(run({"supported": True, "answer": "Credits cover 30 percent [3].", "citations": [3]}))
    assert good.mode == "llm" and good.retrieval == "hybrid" and good.citations == [3]
    bad = asyncio.run(run({"supported": True, "answer": "Made up [7].", "citations": [7]}))
    assert bad.mode == "extractive" and "out-of-report" in bad.reason


def test_ask_report_records_retrieval_in_history(tmp_path: Path) -> None:
    class Store:
        def __init__(self) -> None:
            self.rows: list[tuple[str, str, dict[str, Any] | None]] = []

        def add_chat(self, _rid: str, role: str, text: str, meta: dict[str, Any] | None = None) -> int:
            self.rows.append((role, text, meta))
            return len(self.rows)

    store = Store()
    asyncio.run(ask_report(store, "r1", "tax credits share of cost?", REPORT, None, service(tmp_path)))
    assert store.rows[1][2] is not None and store.rows[1][2]["retrieval"] == "hybrid"


# ----- embedders ---------------------------------------------------------------------------------
def test_local_st_embedder_normalises_via_model() -> None:
    class Model:
        def get_sentence_embedding_dimension(self) -> int:
            return 3

        def encode(self, texts: list[str], **kw: Any) -> list[list[float]]:
            assert kw["normalize_embeddings"] is True
            return [[1.0, 0.0, 0.0] for _ in texts]

    emb = LocalSTEmbedder("all-MiniLM-L6-v2", Model())
    assert emb.dimension == 3 and emb.embed(["a", "b"]) == [[1.0, 0.0, 0.0]] * 2 and emb.embed([]) == []


def test_api_embedder_posts_openai_format_and_is_free_only() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append({"url": str(request.url), "auth": request.headers.get("authorization"), **body})
        data = [{"index": i, "embedding": [3.0, 4.0]} for i in reversed(range(len(body["input"])))]
        return httpx.Response(200, json={"data": data})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    emb = OpenAICompatEmbedder("https://openrouter.ai/api/v1/", "nvidia/nemotron-3-embed-1b:free", "k", client=client, batch_size=2)
    vecs = emb.embed(["a", "b", "c"])
    assert vecs == [[0.6, 0.8]] * 3 and emb.dimension == 2 and len(seen) == 2
    assert seen[0]["url"] == "https://openrouter.ai/api/v1/embeddings" and seen[0]["model"].endswith(":free")
    assert seen[0]["auth"] == "Bearer k"
    with pytest.raises(EmbeddingUnavailable, match="not free"):
        OpenAICompatEmbedder("https://openrouter.ai/api/v1", "openai/text-embedding-3-large")
    assert free_api_model("http://localhost:11434/v1", "nomic-embed-text")


@pytest.mark.parametrize("response", [httpx.Response(429, json={}), httpx.Response(200, json={"data": [{"x": 1}]}),
                                      httpx.Response(200, json={"data": []})])
def test_api_embedder_errors_are_reported_without_body(response: httpx.Response) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: response))
    with pytest.raises(EmbeddingUnavailable):
        OpenAICompatEmbedder("http://localhost:11434/v1", "m", client=client).embed(["a"])


def test_missing_embeddings_deps_mean_no_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda n, *a: None if n == "sentence_transformers" else real(n, *a))
    st = settings(tmp_path)
    assert embeddings_ready(st)[0] is False and build_embedder(st) is None
    assert build_service(st) is None
    assert status(st)["retrieval"] == "word overlap (fallback)"


def test_disabled_by_default(tmp_path: Path) -> None:
    st = RagSettings(_env_file=None)  # type: ignore[call-arg]
    assert st.enabled is False and st.index == "auto" and st.embeddings == "local"
    assert build_service(st) is None


def test_api_provider_needs_key_unless_local(tmp_path: Path) -> None:
    assert embeddings_ready(settings(tmp_path, embeddings="api"))[0] is False  # no key in tests
    assert embeddings_ready(settings(tmp_path, embeddings="api", api_base="http://127.0.0.1:11434/v1",
                                     api_model="nomic-embed-text"))[0] is True
    assert embeddings_ready(settings(tmp_path, embeddings="api", api_model="openai/text-embedding-3-small",
                                     api_key="k"))[1].endswith("not a free model")


# ----- index selection ---------------------------------------------------------------------------
def test_open_index_falls_back_to_local(tmp_path: Path) -> None:
    st = settings(tmp_path, index="auto")
    idx, why = open_index(st, "m", database_url="")
    assert isinstance(idx, LocalIndex) and why == "MDRA_DATABASE_URL not set"

    class Broken:
        def begin(self) -> Any:
            raise OSError("refused")

    idx, why = open_index(st, "m", engine=Broken())
    assert isinstance(idx, LocalIndex) and why == "pgvector extension not available"
    assert open_index(settings(tmp_path), "m", database_url="postgresql://x")[1] == "MDRA_RAG_INDEX=local"


def test_sqlite_is_not_pgvector(tmp_path: Path) -> None:
    sa = pytest.importorskip("sqlalchemy")
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'x.db'}")
    assert PgVectorIndex.available(engine) is False


def test_vector_literal_rejects_nan() -> None:
    assert _vec_literal([0.5, -1.0]) == "[0.5,-1]"
    with pytest.raises(ValueError):
        _vec_literal([float("nan")])


@pytest.mark.skipif(not os.environ.get("MDRA_TEST_DATABASE_URL"), reason="set MDRA_TEST_DATABASE_URL (Postgres + pgvector)")
def test_pgvector_round_trip() -> None:
    sa = pytest.importorskip("sqlalchemy")
    from deep_research.persistence.sqlalchemy_repository import normalize_database_url

    engine = sa.create_engine(normalize_database_url(os.environ["MDRA_TEST_DATABASE_URL"]))
    if not PgVectorIndex.available(engine):
        pytest.skip("pgvector extension not installed on the test server")
    emb = HashingEmbedder(64)
    idx = PgVectorIndex(engine, "test-hashing-64")
    rag = RagService(RagSettings(_env_file=None, enabled=True), emb, idx)  # type: ignore[call-arg]
    assert rag.index_report("pg-r1", REPORT, force=True) > 0
    assert rag.index_report("pg-r1", REPORT) == 0
    hits = rag.search("tax credits share of cost", run_id="pg-r1")
    assert hits and hits[0].chunk.run_id == "pg-r1" and hits[0].cosine is not None
    assert all(c.run_id == "pg-r1" for c in idx.chunks("pg-r1"))
    n = idx.count()
    rag.index_report("pg-r1", {**REPORT, "content": "## A\nOne sentence only about ducts [1]."})
    assert idx.count() < n
    empty = {"content": "", "citations": [], "claim_scores": []}
    assert rag.index_report("pg-empty", empty, force=True) == 0 and rag.last_reindexed
    assert idx.doc_hash("pg-empty") == report_hash(empty) and idx.chunks("pg-empty") == []
    assert rag.index_report("pg-empty", empty) == 0 and not rag.last_reindexed


# ----- backfill CLI ------------------------------------------------------------------------------
def _write_runs(folder: Path) -> None:
    folder.mkdir()
    (folder / "a.json").write_text(json.dumps({"run_id": "a", "params": {"objective": "Heat pumps"},
                                               "result": {"report": REPORT}}), encoding="utf-8")
    (folder / "b.json").write_text(json.dumps({"run_id": "b", "status": "failed"}), encoding="utf-8")
    (folder / "bad.json").write_text("{", encoding="utf-8")


def test_backfill_indexes_saved_reports_once(tmp_path: Path) -> None:
    _write_runs(tmp_path / "runs")
    runs = saved_runs(tmp_path / "runs")
    assert [r["run_id"] for r in runs] == ["a", "b"]
    rag = service(tmp_path)
    first = backfill(rag, runs)
    assert (first.indexed, first.no_report, first.failed) == (1, 1, 0) and first.chunks == rag.index.count()
    again = backfill(rag, runs)
    assert (again.indexed, again.up_to_date) == (0, 1)
    assert backfill(rag, runs, force=True).indexed == 1
    assert rag.index.chunks("a")[0].title == "Heat pumps"


def test_backfill_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import deep_research.rag.service as svc
    from deep_research import cli

    _write_runs(tmp_path / "runs")
    rag = service(tmp_path)
    monkeypatch.setattr(svc, "build_service", lambda *_a, **_k: rag)
    assert cli.main(["rag-backfill", "--runs-dir", str(tmp_path / "runs")]) == 0
    err = capsys.readouterr().err
    assert "indexed 1" in err and "no report 1" in err and "MDRA_RAG_ENABLED is off" in err
    monkeypatch.setattr(svc, "build_service", lambda *_a, **_k: None)
    assert cli.main(["rag-backfill", "--runs-dir", str(tmp_path / "runs")]) == 2


# ----- Knowledge Base ----------------------------------------------------------------------------
def test_kb_rag_search_indexes_and_links_runs(tmp_path: Path) -> None:
    from deep_research.dashboard.core import rag_line, rag_search

    runs = [{"run_id": "a", "params": {"objective": "Heat pumps"}, "result": {"report": REPORT}}, {"run_id": "b"}]
    rag = service(tmp_path)
    hits = rag_search(rag, runs, "tax credit")
    assert hits and hits[0]["run_id"] == "a" and hits[0]["backend"].startswith("hybrid BM25")
    assert "[3]" in hits[0]["text"]
    assert rag_search(None, runs, "tax") == [] and rag_line(rag).startswith("hybrid")
    assert rag_line(None).startswith("off")


# ----- hard word cap, install hint, empty reports ------------------------------------------------
TELUGU = "తెలుగు భాష దక్షిణ భారతదేశంలో మాట్లాడే ద్రావిడ భాష ఇది ఆంధ్రప్రదేశ్ తెలంగాణ రాష్ట్రాల అధికార భాష"


@pytest.mark.parametrize("text", [" ".join([TELUGU] * 60), " ".join(["lowercase words without stops"] * 300)])
def test_boundaryless_text_is_cut_into_capped_overlapping_windows(text: str) -> None:
    rep = {"content": text, "citations": []}
    body = [c for c in chunk_report("r", rep, chunk_words=120, overlap_words=30) if c.kind == "report"]
    words = text.split()
    assert len(body) > 5 and max(len(c.text.split()) for c in body) <= 120
    assert body[0].text.split() == words[:120]
    assert body[1].text.split()[:30] == words[90:120]  # 30-word overlap kept between windows
    assert body[-1].text.split()[-1] == words[-1]  # nothing dropped at the end


def test_overlap_never_pushes_a_window_over_the_cap() -> None:
    long = " ".join(f"w{i}" for i in range(110)) + "."
    rep = {"content": "## A\n" + " ".join(f"Short sentence {i} here." for i in range(10)) + " " + long.capitalize(),
           "citations": []}
    body = [c for c in chunk_report("r", rep, chunk_words=120, overlap_words=30) if c.kind == "report"]
    assert max(len(c.text.split()) for c in body) <= 120


def test_missing_st_hint_names_the_rag_extra(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda n, *a: None if n == "sentence_transformers" else real(n, *a))
    ok, hint = embeddings_ready(settings(tmp_path))
    assert not ok and ".[rag]" in hint and ".[memory]" not in hint


EMPTY: dict[str, Any] = {"content": "", "citations": [], "claim_scores": []}


def test_empty_report_hash_is_stored_and_force_reprocesses(tmp_path: Path) -> None:
    rag = service(tmp_path)
    calls: list[int] = []
    orig = rag.embedder.embed
    rag.embedder.embed = lambda texts: calls.append(len(texts)) or orig(texts)  # type: ignore[method-assign]
    runs = [{"run_id": "e", "result": {"report": EMPTY}}]
    first = backfill(rag, runs)
    assert (first.indexed, first.up_to_date, first.chunks) == (1, 0, 0)
    assert rag.index.doc_hash("e") == report_hash(EMPTY) and rag.index.count() == 0
    again = backfill(rag, runs)
    assert (again.indexed, again.up_to_date) == (0, 1) and rag.last_reindexed is False
    reopened = LocalIndex(tmp_path / "rag", "hashing-256")  # persisted, not just cached
    assert reopened.doc_hash("e") == report_hash(EMPTY)
    forced = backfill(rag, runs, force=True)
    assert (forced.indexed, forced.up_to_date) == (1, 0) and rag.last_reindexed is True
    assert calls == []  # 0 chunks: never sent anything to the embedder


def test_report_that_becomes_empty_drops_old_chunks(tmp_path: Path) -> None:
    rag = service(tmp_path)
    assert rag.index_report("r", REPORT) > 0
    assert rag.index_report("r", EMPTY) == 0 and rag.last_reindexed
    assert rag.index.chunks("r") == [] and rag.index.doc_hash("r") == report_hash(EMPTY)
    assert rag.index_report("r", EMPTY) == 0 and not rag.last_reindexed


def test_index_without_docs_file_still_reads_chunk_hash(tmp_path: Path) -> None:
    rag = service(tmp_path)
    rag.index_report("r", REPORT)
    idx = LocalIndex(tmp_path / "rag", "hashing-256")
    idx.docs_path.write_text("not json", encoding="utf-8")  # corrupt/legacy docs.json -> fall back to chunks
    assert idx.doc_hash("r") == report_hash(REPORT)
