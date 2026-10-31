"""Testing Bot: phase 7 (C) edge cases for GraphRAG and the span store."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from deep_research.scale import graphrag, spanstore
from deep_research.scale.graph import build_graph, sync_neo4j
from deep_research.scale.graphrag import (GraphSettings, LocalGraphBackend, claim_id, expand, open_backend,
                                          report_input)

FAKE_KEY = "sk-or-v1-" + "a" * 48

REPORT: dict[str, Any] = {
    "title": "T",
    "citations": [{"number": 1, "url": "https://a.example/x", "title": "A"},
                  {"number": 2, "url": "https://b.example/y", "title": "B"}],
    "claim_scores": [
        {"text": "NASA launched Artemis in 2022.", "citations": [1]},
        {"text": "NASA delayed the second mission.", "citations": [1]},
        {"text": "NASA has no citation here.", "citations": [9]},
        {"text": "Unrelated source two claim.", "citations": [2]},
    ],
}


class FakeSession:
    def __init__(self, log: list[tuple[str, dict[str, Any]]]) -> None:
        self.log = log

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def run(self, q: str, **kw: Any) -> list[Any]:
        self.log.append((q, kw))
        return []


class FakeDriver:
    def __init__(self) -> None:
        self.log: list[tuple[str, dict[str, Any]]] = []

    def session(self, database: Any = None) -> FakeSession:
        return FakeSession(self.log)

    def verify_connectivity(self) -> None:
        raise ConnectionError("down")

    def close(self) -> None:
        return None


def test_sync_uses_parameters_not_formatting() -> None:
    evil = {**REPORT, "claim_scores": [{"text": "x'}) DETACH DELETE n //", "citations": [1]}]}
    d = FakeDriver()
    sync_neo4j(build_graph([report_input("r1'}) MATCH (n) DETACH DELETE n //", evil)]), d)
    for q, _ in d.log:
        assert "DETACH" not in q and "r1'" not in q


def test_sync_run_lists_sorted_unique() -> None:
    d = FakeDriver()
    g = build_graph([report_input("r1", REPORT), report_input("r1", REPORT), report_input("r2", REPORT)])
    sync_neo4j(g, d)
    rows = next(kw["rows"] for q, kw in d.log if "SET n.kind" in q)
    for r in rows:
        assert r["runs"] == sorted(set(r["runs"]))


def test_unreachable_neo4j_falls_back_to_local() -> None:
    backend, note = open_backend("r1", REPORT, GraphSettings(backend="auto"), driver=FakeDriver())
    assert isinstance(backend, LocalGraphBackend) and "ConnectionError" in note and "down" not in note


def test_expand_drops_uncited_and_other_run_claims() -> None:
    g = build_graph([report_input("r1", REPORT), report_input("r2", {**REPORT, "claim_scores": [
        {"text": "NASA other run claim.", "citations": [1]}]})])
    out = expand("r1", REPORT, [(1.0, [claim_id("NASA launched Artemis in 2022.")])], LocalGraphBackend(g),
                 GraphSettings(max_neighbours=10))
    texts = {row["text"] for _, row, _, _ in out}
    assert "NASA has no citation here." not in texts
    assert "NASA other run claim." not in texts
    assert "NASA launched Artemis in 2022." not in texts
    assert "NASA delayed the second mission." in texts
    for score, _, rel, _ in out:
        assert score == pytest.approx(0.5 * graphrag.REL_FACTOR[rel])


def test_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("MDRA_GRAPH_RAG", "MDRA_GRAPH_AUTO_SYNC", "MDRA_TRACE_STORE"):
        monkeypatch.delenv(k, raising=False)
    s = GraphSettings()
    assert not s.rag and not s.auto_sync
    assert spanstore.open_store() is None


@pytest.mark.parametrize("fmt", ["sqlite", "jsonl"])
def test_store_roundtrip_and_traversal(tmp_path: Any, fmt: str) -> None:
    st = spanstore.SpanStore(tmp_path / "t", fmt)
    row = {"run_id": "../../etc/evil", "trace_id": "t", "span_id": "s1", "parent_id": "", "name": "n",
           "start_ns": 2, "end_ns": 5, "status": "OK", "error": "", "attrs": {}}
    st.write([row, {**row, "span_id": "s0", "start_ns": 1}, row])
    got = st.load("../../etc/evil")
    assert [r["span_id"] for r in got] == ["s0", "s1"]
    assert all(p.resolve().is_relative_to((tmp_path / "t").resolve()) for p in tmp_path.rglob("*") if p.is_file())


def _span(attrs: dict[str, Any], desc: str = "", name: str = "op") -> Any:
    return SimpleNamespace(
        attributes=attrs, name=name, parent=None, start_time=1, end_time=2,
        get_span_context=lambda: SimpleNamespace(trace_id=1, span_id=2),
        status=SimpleNamespace(status_code=SimpleNamespace(name="ERROR" if desc else "OK"), description=desc))


def test_span_row_redacts_attrs() -> None:
    row = spanstore.span_row(_span({"mdra.run_id": "r", "api_key": FAKE_KEY,
                                    "url": "https://user:pw@h.example/p", "q": f"Bearer {FAKE_KEY}"}))
    assert row is not None
    blob = repr(row)
    assert FAKE_KEY not in blob and "user:pw" not in blob


def test_span_row_without_run_is_dropped() -> None:
    assert spanstore.span_row(_span({"x": 1})) is None


def test_span_row_redacts_error_description() -> None:
    row = spanstore.span_row(_span({"mdra.run_id": "r"}, desc=f"failed GET https://u:secretpw@h.example key={FAKE_KEY}"))
    assert row is not None
    assert FAKE_KEY not in row["error"] and "secretpw" not in row["error"]


def test_span_row_redacts_name() -> None:
    row = spanstore.span_row(_span({"mdra.run_id": "r"}, name=f"GET https://u:secretpw@h.example?k={FAKE_KEY}"))
    assert row is not None
    assert FAKE_KEY not in row["name"] and "secretpw" not in row["name"]


def test_waterfall_cycle_safe() -> None:
    rows = spanstore.waterfall([
        {"span_id": "a", "parent_id": "b", "name": "a", "start_ns": 0, "end_ns": 10},
        {"span_id": "b", "parent_id": "a", "name": "b", "start_ns": 5, "end_ns": 3}])
    assert len(rows) == 2 and all(r["duration_ms"] >= 0 for r in rows)
