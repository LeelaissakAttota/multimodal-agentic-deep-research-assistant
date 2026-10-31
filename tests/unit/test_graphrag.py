"""GraphRAG: entities/contradictions in the graph, neighbour lookup (local + fake Neo4j), chat expansion,
auto-sync and the graph-sync CLI. Neo4j is always a fake driver here (no network)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deep_research.assistant import followup
from deep_research.rag.chunking import Chunk
from deep_research.rag.service import Hit
from deep_research.scale import graphrag
from deep_research.scale.graph import build_graph, claim_id, extract_entities
from deep_research.scale.graphrag import (GraphSettings, LocalGraphBackend, Neighbour, Neo4jGraphBackend, auto_sync,
                                          expand, open_backend, report_input, source_ids, sync_runs)

A = "Green tea contains EGCG catechins [1]."
B = "Matcha has more EGCG than sencha [2]."
C = "Green tea raises LDL cholesterol [1]."
D = "Green tea lowers LDL cholesterol [3]."
E = "Uncited claim about World Health Organization."
REPORT: dict[str, Any] = {
    "title": "Tea", "content": "# Tea\n",
    "citations": [{"number": 1, "title": "NIH", "url": "https://nih.gov/tea?token=SECRET"},
                  {"number": 2, "title": "Matcha", "url": "https://matcha.example/a"},
                  {"number": 3, "title": "Cardio", "url": "https://cardio.example/b"}],
    "claim_scores": [{"text": A, "citations": [1]}, {"text": B, "citations": [2]}, {"text": C, "citations": [1]},
                     {"text": D, "citations": [3]}, {"text": E, "citations": [9]}],
    "contradictions": [{"kind": "cross_source", "claims": [C, D], "citations": [[1], [3]]}],
}


def test_extract_entities() -> None:
    assert extract_entities("The European Union and the WHO cite GPT4 studies [1].") == ["European Union", "WHO", "GPT4"]
    assert extract_entities("Tea is good. However Green tea") == []  # single capitalised words are not entities
    assert extract_entities("green tea CONTAINS catechins") == []


def test_graph_has_entities_and_contradictions() -> None:
    g = build_graph([report_input("r1", REPORT)])
    assert (claim_id(C), claim_id(D), "contradicts") in g.edges
    assert (claim_id(A), "entity:" + claim_id("EGCG")[6:], "mentions") in g.edges
    assert g.counts()["entity"] >= 3 and "SECRET" not in repr(g.nodes)


def test_local_neighbours() -> None:
    backend = LocalGraphBackend(build_graph([report_input("r1", REPORT), report_input("r2", {
        "citations": REPORT["citations"][:1], "claim_scores": [{"text": "Other run EGCG claim.", "citations": [1]}]})]))
    found = backend.neighbours("r1", [claim_id(C)], 10)
    rels = {(n.claim_id, n.relation) for n in found}
    assert (claim_id(D), "contradicts") in rels and (claim_id(A), "shared_source") in rels
    assert all(n.claim_id != claim_id("Other run EGCG claim.") for n in found)  # other runs never leak in
    assert {n.claim_id for n in backend.neighbours("r1", [claim_id(A)], 10)} >= {claim_id(B), claim_id(C)}
    assert {n.claim_id for n in backend.neighbours("r1", source_ids(REPORT, [3]), 10)} == {claim_id(D)}
    assert source_ids(REPORT, [1]) == ["source:https://nih.gov/tea"]


def test_expand_weights_and_valid_citations() -> None:
    backend = LocalGraphBackend(build_graph([report_input("r1", REPORT)]))
    s = GraphSettings(rag=True, weight=0.5)
    out = expand("r1", REPORT, [(1.0, [claim_id(C)])], backend, s)
    by_text = {row["text"]: (score, rel) for score, row, rel, _ in out}
    assert by_text[D] == (0.5, "contradicts") and by_text[A] == (0.4, "shared_source")
    assert C not in by_text and E not in by_text  # the seed itself and uncited claims are never added
    assert expand("r1", REPORT, [(1.0, [claim_id(C)])], backend, GraphSettings(max_neighbours=0)) == []
    assert len(expand("r1", REPORT, [(1.0, [claim_id(C)])], backend, GraphSettings(max_neighbours=1))) == 1


class FakeSession:
    def __init__(self, driver: FakeDriver) -> None:
        self.driver = driver

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def run(self, query: str, **params: Any) -> list[dict[str, Any]]:
        self.driver.log.append((query, params))
        return list(self.driver.rows) if "RETURN sid" in query else []


class FakeDriver:
    def __init__(self, rows: list[dict[str, Any]] | None = None, fail: bool = False) -> None:
        self.rows = rows or []
        self.fail = fail
        self.log: list[tuple[str, Any]] = []
        self.closed = False

    def session(self, database: str | None = None) -> FakeSession:
        return FakeSession(self)

    def verify_connectivity(self) -> None:
        if self.fail:
            raise ConnectionError("bolt://neo4j:hunter2pw@db:7687 refused")

    def close(self) -> None:
        self.closed = True


def test_neo4j_backend_query_and_order() -> None:
    rows = [{"sid": claim_id(C), "id": claim_id(A), "rel": "shared_source"},
            {"sid": claim_id(C), "id": claim_id(D), "rel": "contradicts"},
            {"sid": claim_id(C), "id": claim_id(D), "rel": "shared_entity"}]
    driver = FakeDriver(rows)
    found = Neo4jGraphBackend(driver).neighbours("r1", [claim_id(C)], 5)
    assert found == [Neighbour(claim_id(D), "contradicts", claim_id(C)), Neighbour(claim_id(A), "shared_source", claim_id(C))]
    query, params = driver.log[0]
    assert "$run IN c.runs" in query and params["run"] == "r1" and params["seeds"] == [claim_id(C)]


def test_open_backend_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    backend, note = open_backend("r1", REPORT, GraphSettings())
    assert backend.name == "local" and note == ""  # Neo4j not configured
    backend, note = open_backend("r1", REPORT, GraphSettings(), driver=FakeDriver(fail=True))
    assert backend.name == "local" and "ConnectionError" in note and "hunter2pw" not in note
    backend, note = open_backend("r1", REPORT, GraphSettings(), driver=FakeDriver())
    assert backend.name == "neo4j" and note == ""
    backend, note = open_backend("r1", REPORT, GraphSettings(backend="local"), driver=FakeDriver())
    assert backend.name == "local"
    backend, note = open_backend("r1", REPORT, GraphSettings(backend="neo4j"))
    assert backend.name == "local" and "not configured" in note
    driver = FakeDriver()
    open_backend("r1", REPORT, GraphSettings(auto_sync=True), driver=driver)
    assert any("MERGE (n:MdraNode" in q for q, _ in driver.log)  # synced before querying


class FakeRag:
    def __init__(self, hits: list[Hit]) -> None:
        self.hits = hits

    def index_report(self, run_id: str, report: dict[str, Any]) -> int:
        return 0

    def search(self, query: str, run_id: str | None = None) -> list[Hit]:
        return self.hits

    def supported(self, hit: Hit, min_overlap: float) -> bool:
        return bool(hit.chunk.citations)


def _hit(text: str, cites: list[int], score: float) -> Hit:
    return Hit(Chunk("c", "r1", "claim", 0, text, cites), score, 1.0, 0.9, 0.8)


def test_chat_graph_expansion_off_by_default() -> None:
    ans = followup.extractive_answer("Does green tea raise LDL?", REPORT, FakeRag([_hit(C, [1], 0.03)]), "r1")  # type: ignore[arg-type]
    assert ans.retrieval == "hybrid" and all(not p.kind.startswith("graph") for p in ans.passages)


def test_chat_graph_expansion_adds_contradiction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDRA_GRAPH_RAG", "true")
    rag = FakeRag([_hit(C, [1], 0.03)])
    good, ranked = followup.hybrid_passages("LDL?", REPORT, rag, "r1")  # type: ignore[arg-type]
    assert ranked[0].text == C and ranked[1].text == D and ranked[1].kind == "graph:contradicts"
    assert ranked[1].citations == [3] and D in [p.text for p in good]
    ans = followup.extractive_answer("Does green tea raise LDL?", REPORT, rag, "r1")  # type: ignore[arg-type]
    assert ans.retrieval == "hybrid+graph" and ans.citations == [1, 3] and "graph local: +" in ans.reason
    assert set(ans.citations) <= {1, 2, 3}


def test_chat_graph_expansion_errors_are_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDRA_GRAPH_RAG", "true")

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("bolt://u:hunter2pw@h")

    monkeypatch.setattr(graphrag, "open_backend", boom)
    ans = followup.extractive_answer("Does green tea raise LDL?", REPORT, FakeRag([_hit(C, [1], 0.03)]), "r1")  # type: ignore[arg-type]
    assert ans.supported and ans.retrieval == "hybrid" and "RuntimeError" in ans.reason and "hunter2pw" not in ans.reason


def test_sync_runs_and_auto_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("neo4j")  # needs the optional `graph` extra (not installed in CI)
    driver = FakeDriver()
    res = sync_runs([{"run_id": "r1", "result": {"report": REPORT}}, {"run_id": "r2", "status": "failed"}], driver)
    assert res["runs"] == 1 and res["nodes"] > 5
    assert auto_sync("r1", {"report": REPORT}) is False  # off by default
    monkeypatch.setenv("MDRA_GRAPH_AUTO_SYNC", "true")
    assert auto_sync("r1", {"report": REPORT}) is False  # Neo4j not configured
    monkeypatch.setenv("MDRA_NEO4J_URI", "bolt://127.0.0.1:9")
    calls: list[str] = []
    monkeypatch.setattr(graphrag, "sync_run", lambda rid, report, title="", driver=None: calls.append(rid) or {})
    assert auto_sync("r1", {"report": REPORT}) is True and calls == ["r1"]
    assert auto_sync("r1", {"no": "report"}) is False

    def boom(*a: Any, **k: Any) -> Any:
        raise OSError("refused")

    monkeypatch.setattr(graphrag, "sync_run", boom)
    assert auto_sync("r1", {"report": REPORT}) is False  # never raises


def test_graph_sync_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    pytest.importorskip("neo4j")  # needs the optional `graph` extra (not installed in CI)
    import json

    from deep_research.cli import main

    (tmp_path / "r1.json").write_text(json.dumps({"run_id": "r1", "result": {"report": REPORT}}), encoding="utf-8")
    assert main(["graph-sync", "--runs-dir", str(tmp_path)]) == 2
    monkeypatch.setenv("MDRA_NEO4J_URI", "bolt://neo4j:hunter2pw@127.0.0.1:9")
    seen: list[int] = []
    monkeypatch.setattr(graphrag, "sync_runs", lambda runs, driver=None: seen.append(len(runs)) or
                        {"runs": 1, "nodes": 9, "edges": 12})
    assert main(["graph-sync", "--runs-dir", str(tmp_path)]) == 0 and seen == [1]

    def boom(*a: Any, **k: Any) -> Any:
        raise ConnectionError("bolt://neo4j:hunter2pw@127.0.0.1:9")

    monkeypatch.setattr(graphrag, "sync_runs", boom)
    assert main(["graph-sync", "--runs-dir", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "9 nodes, 12 links" in err and "ConnectionError" in err and "hunter2pw" not in err


def test_dashboard_graph_inputs_use_raw_claims() -> None:
    from deep_research.dashboard import core

    g = core.knowledge_graph([{"run_id": "r1", "status": "completed", "result": {"report": REPORT}}])
    assert claim_id(C) in g.nodes and (claim_id(C), claim_id(D), "contradicts") in g.edges
