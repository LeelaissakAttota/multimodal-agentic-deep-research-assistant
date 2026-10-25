"""Phase 7 (C) fixes: trace retention, per-run Neo4j edge replacement, trace timeline for failed runs."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from deep_research.scale.graph import build_graph, sync_neo4j
from deep_research.scale.graphrag import report_input
from deep_research.scale.spanstore import SpanStore

NOW = 1_800_000_000.0
DAY = 86400


def _row(rid: str, sid: str, end_s: float) -> dict[str, Any]:
    return {"run_id": rid, "trace_id": "t", "span_id": sid, "parent_id": "", "name": "mdra.run",
            "start_ns": int((end_s - 1) * 1e9), "end_ns": int(end_s * 1e9), "status": "UNSET", "error": "", "attrs": {}}


def _sqlite(tmp_path: Path, **kw: Any) -> SpanStore:
    store = SpanStore(tmp_path, "sqlite", prune_interval=0, **kw)
    store.write([_row("old", "a", NOW - 30 * DAY), _row("mid", "b", NOW - 2 * DAY), _row("new", "c", NOW - 60),
                 _row("new", "d", NOW - 10)])
    return store


def test_sqlite_prune_by_age_and_count(tmp_path: Path) -> None:
    store = _sqlite(tmp_path, max_age_days=14)
    assert store.prune(now=NOW) == ["old"]
    assert store.load("old") == [] and len(store.load("new")) == 2 and store.load("mid")
    store.max_runs = 1
    assert store.prune(now=NOW) == ["mid"] and len(store.load("new")) == 2
    assert store.prune(now=NOW) == []


def test_prune_disabled_keeps_everything(tmp_path: Path) -> None:
    store = _sqlite(tmp_path)
    assert store.prune(now=NOW) == [] and store.load("old")
    assert SpanStore(tmp_path / "empty", "sqlite", max_runs=1).prune() == []  # no db yet


def test_jsonl_prune_by_file_age_and_count(tmp_path: Path) -> None:
    store = SpanStore(tmp_path, "jsonl", prune_interval=0)
    for rid in ("a", "b", "c"):
        store.write([_row(rid, "s", NOW)])
    for rid, age in (("a", 30 * DAY), ("b", 2 * DAY), ("c", 0)):
        os.utime(tmp_path / f"{rid}.jsonl", (NOW - age, NOW - age))
    (tmp_path / "notes.txt").write_text("keep", encoding="utf-8")
    store.max_age_days = 14
    assert store.prune(now=NOW) == ["a"] and not (tmp_path / "a.jsonl").exists()
    store.max_runs = 1
    assert store.prune(now=NOW) == ["b"] and (tmp_path / "c.jsonl").exists() and (tmp_path / "notes.txt").exists()


def test_periodic_prune_on_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SpanStore(tmp_path, "sqlite", max_runs=1, prune_interval=3600)
    store.write([_row("r1", "a", time.time() - 100)])
    store.write([_row("r2", "b", time.time())])
    assert store.load("r1")  # interval not reached since the first write's prune
    monkeypatch.setattr(store, "_last_prune", time.monotonic() - 7200)
    store.write([_row("r2", "c", time.time())])
    assert store.load("r1") == [] and len(store.load("r2")) == 2


def test_prune_errors_never_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SpanStore(tmp_path, "sqlite", max_runs=1)

    def boom(*a: Any, **k: Any) -> Any:
        raise OSError("disk")

    monkeypatch.setattr(store, "prune", boom)
    assert store.maybe_prune(force=True) == []


@pytest.fixture
def _tracing() -> Iterator[None]:
    pytest.importorskip("opentelemetry.sdk")
    from deep_research.scale.tracing import reset_tracing

    reset_tracing()
    yield
    reset_tracing()


@pytest.mark.usefixtures("_tracing")
def test_startup_prune_from_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from deep_research.scale.tracing import tracing_enabled

    SpanStore(tmp_path, "sqlite").write([_row("ancient", "a", time.time() - 400 * DAY), _row("fresh", "b", time.time())])
    monkeypatch.setenv("MDRA_TRACE_STORE", "sqlite")
    monkeypatch.setenv("MDRA_TRACE_DIR", str(tmp_path))
    monkeypatch.setenv("MDRA_TRACE_MAX_AGE_DAYS", "30")
    assert tracing_enabled()
    store = SpanStore(tmp_path, "sqlite")
    assert store.load("ancient") == [] and store.load("fresh")


def test_trace_retention_defaults() -> None:
    from deep_research.scale.config import ScaleSettings

    s = ScaleSettings()
    assert (s.trace_max_age_days, s.trace_max_runs, s.trace_prune_interval_seconds) == (14.0, 500, 3600.0)


# ----- Neo4j: per-run edge replacement ------------------------------------------------------------------
class TxDriver:
    """Fake driver whose session exposes ``execute_write`` (one transaction) and records every query."""

    def __init__(self) -> None:
        self.log: list[tuple[str, dict[str, Any]]] = []
        self.tx_calls = 0
        self.closed = False

    def session(self, database: Any = None) -> TxDriver:
        return self

    def __enter__(self) -> TxDriver:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def run(self, q: str, **kw: Any) -> list[Any]:
        self.log.append((q, kw))
        return []

    def execute_write(self, fn: Any) -> Any:
        self.tx_calls += 1
        return fn(self)

    def close(self) -> None:
        self.closed = True


REPORT = {"citations": [{"number": 1, "title": "A", "url": "https://a.example/x"}],
          "claim_scores": [{"text": "Tea raises LDL.", "citations": [1]}, {"text": "Tea lowers LDL.", "citations": [1]}],
          "contradictions": [{"claims": ["Tea raises LDL.", "Tea lowers LDL."]}]}


def test_sync_replaces_run_edges_in_one_transaction() -> None:
    d = TxDriver()
    sync_neo4j(build_graph([report_input("r1", REPORT)]), d)
    assert d.tx_calls == 1 and "CREATE CONSTRAINT" in d.log[0][0]
    queries = [q for q, _ in d.log[1:]]
    forget = next(kw for q, kw in d.log if "NOT x IN $runs" in q)
    assert forget["runs"] == ["r1"]
    edges = next(kw["rows"] for q, kw in d.log if "MERGE (a)-[e:REL" in q)
    assert all(e["runs"] == ["r1"] for e in edges) and any(e["rel"] == "contradicts" for e in edges)
    assert any("size(e.runs) = 0 DELETE e" in q and "mdra_pruned" in q for q in queries)
    assert queries.index(next(q for q in queries if "NOT x IN $runs" in q)) < \
        queries.index(next(q for q in queries if "MERGE (a)-[e:REL" in q)) < \
        queries.index(next(q for q in queries if "DELETE e" in q))
    assert not any("DETACH" in q for q in queries)


def test_edge_runs_tracked_per_run() -> None:
    g = build_graph([report_input("r1", REPORT), report_input("r2", {**REPORT, "contradictions": []})])
    contra = next(k for k in g.edges if k[2] == "contradicts")
    assert g.edge_runs[contra] == {"r1"}
    support = next(k for k in g.edges if k[2] == "supported_by")
    assert g.edge_runs[support] == {"r1", "r2"}


@pytest.mark.skipif(not os.environ.get("MDRA_TEST_NEO4J_URI"), reason="set MDRA_TEST_NEO4J_URI to test real Neo4j")
def test_real_neo4j_stale_edges_removed() -> None:  # pragma: no cover - needs a service
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(os.environ["MDRA_TEST_NEO4J_URI"],
                                  auth=("neo4j", os.environ.get("MDRA_TEST_NEO4J_PASSWORD", "")))

    def edges(run: str | None = None) -> set[tuple[str, str, tuple[str, ...]]]:
        with driver.session() as s:
            rows = s.run("MATCH (a:MdraNode)-[e:REL]->(b:MdraNode) WHERE a.id STARTS WITH 'report:t' OR "
                         "b.kind IN ['claim','source','entity'] RETURN a.id AS a, e.type AS t, e.runs AS r")
            return {(r["a"], r["t"], tuple(r["r"] or [])) for r in rows}

    try:
        with driver.session() as s:
            s.run("MATCH (n:MdraNode) DETACH DELETE n")  # throwaway test database
        other = {"citations": REPORT["citations"], "claim_scores": REPORT["claim_scores"][:1]}
        sync_neo4j(build_graph([report_input("t1", REPORT)]), driver)
        sync_neo4j(build_graph([report_input("t2", other)]), driver)
        before = edges()
        assert any(t == "contradicts" for _, t, _ in before)
        # t1 re-synced without its contradiction and without the second claim/citation
        sync_neo4j(build_graph([report_input("t1", {**REPORT, "contradictions": [],
                                                     "claim_scores": REPORT["claim_scores"][:1]})]), driver)
        after = edges()
        assert not any(t == "contradicts" for _, t, _ in after)
        assert ("report:t2", "cites", ("t2",)) in after and ("report:t2", "makes", ("t2",)) in after
        assert all(r for _, _, r in after)  # no edge left with an empty run list
        with driver.session() as s:
            assert s.run("MATCH ()-[e:REL]->() WHERE e.mdra_pruned RETURN count(e) AS n").single()["n"] == 0
            s.run("MATCH (n:MdraNode) DETACH DELETE n")
    finally:
        driver.close()


# ----- dashboard: trace timeline for failed runs ----------------------------------------------------------
def test_failed_run_shows_trace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    testing = pytest.importorskip("streamlit.testing.v1")
    from deep_research.dashboard.core import RunStore
    from tests.unit.test_dashboard_assistant import APP

    monkeypatch.setenv("DEEP_RESEARCH_DASHBOARD_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("DEEP_RESEARCH_CONTROL_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("DEEP_RESEARCH_ASSISTANT_DB", str(tmp_path / "assistant.db"))
    monkeypatch.setenv("MDRA_TRACE_STORE", "sqlite")
    monkeypatch.setenv("MDRA_TRACE_DIR", str(tmp_path / "traces"))
    RunStore(tmp_path / "runs").save({"run_id": "bad1", "status": "failed", "error": "boom",
                                      "created_at": "2026-10-24T10:00:00+00:00", "params": {"objective": "Tea"}})
    now = time.time()
    SpanStore(tmp_path / "traces").write([
        {**_row("bad1", "a", now), "name": "mdra.run"},
        {**_row("bad1", "b", now), "parent_id": "a", "name": "mdra.tool", "status": "ERROR", "error": "TimeoutError",
         "attrs": {"mdra.tool": "web"}}])
    for view in ("report", "live"):
        at = testing.AppTest.from_file(APP, default_timeout=30)
        at.query_params.update({"page": "researches", "run": "bad1", "view": view})
        at.run()
        assert not at.exception
        text = " ".join(m.value for m in at.markdown) + " ".join(c.value for c in at.caption)
        assert "2 spans" in text and "1 with errors" in text and "TimeoutError" in text, view
