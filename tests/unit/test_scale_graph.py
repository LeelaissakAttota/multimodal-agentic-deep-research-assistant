"""Knowledge graph from claims + sources: local build, DOT, networkx, optional Neo4j (fake driver), KB page."""

from __future__ import annotations

import builtins
from pathlib import Path
from typing import Any

import pytest

from deep_research.dashboard import core
from deep_research.dashboard.core import RunStore
from deep_research.scale import backend_status
from deep_research.scale.graph import build_graph, clean_url, sync_neo4j, to_dot, to_networkx, top_sources

ROOT = Path(__file__).resolve().parents[2]


def _reports() -> list[dict[str, Any]]:
    cites = [{"number": 1, "title": "Tea study", "url": "https://www.nih.gov/tea?token=SECRET123"},
             {"number": 2, "title": 'Quote "x"', "url": "https://user:pw@example.org/a#frag"}]
    return [
        {"run_id": "r1", "title": "Green tea", "citations": cites,
         "claims": [{"text": "Green tea contains catechins.", "citations": [1, 2], "label": "supported"},
                    {"text": "Tea lowers LDL.", "citations": [1, 9]}, {"text": "  "}]},
        {"run_id": "r2", "title": "Tea again", "citations": cites[:1],
         "claims": [{"text": "green tea CONTAINS catechins", "citations": [1]}]},
    ]


def _row() -> dict[str, Any]:
    return next(r for r in backend_status() if r["name"].startswith("Knowledge graph"))


def test_build_merges_claims_and_sources() -> None:
    g = build_graph(_reports())
    assert g.counts() == {"report": 2, "claim": 2, "source": 2, "domain": 2, "edges": 11}
    claim = next(n for n in g.nodes.values() if n["kind"] == "claim" and "catechins" in n["label"])
    assert claim["runs"] == {"r1", "r2"} and claim["verdict"] == "supported"
    assert {n["label"] for n in g.nodes.values() if n["kind"] == "domain"} == {"nih.gov", "example.org"}
    text = repr(g.nodes)
    assert "SECRET123" not in text and "pw@" not in text and "frag" not in text
    assert clean_url("javascript:alert(1)") == "" and clean_url("http://[bad") == ""
    assert top_sources(g)[0] == ("Tea study", 2)


def test_top_sources_without_networkx(monkeypatch: pytest.MonkeyPatch) -> None:
    real = builtins.__import__

    def fake(name: str, *a: Any, **k: Any) -> Any:
        if name == "networkx":
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    assert top_sources(build_graph(_reports()))[0] == ("Tea study", 2)


def test_networkx_and_dot() -> None:
    pytest.importorskip("networkx")
    g = build_graph(_reports())
    nxg = to_networkx(g)
    assert nxg.number_of_nodes() == 8 and nxg.number_of_edges() == 11
    dot = to_dot(g)
    assert dot.startswith("digraph KG {") and dot.endswith("}") and '\\"x\\"' in dot and "supported_by" in dot
    small = to_dot(g, max_nodes=3)
    assert small.count("shape=") == 3


class FakeSession:
    def __init__(self, log: list[tuple[str, Any]]) -> None:
        self.log = log

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def run(self, query: str, **params: Any) -> None:
        self.log.append((query, params))


class FakeDriver:
    def __init__(self) -> None:
        self.log: list[tuple[str, Any]] = []
        self.closed = False

    def session(self, database: str | None = None) -> FakeSession:
        return FakeSession(self.log)

    def close(self) -> None:
        self.closed = True


def test_sync_neo4j_with_fake_driver() -> None:
    driver = FakeDriver()
    assert sync_neo4j(build_graph(_reports()), driver) == {"nodes": 8, "edges": 11}
    (q1, p1), (q2, p2) = driver.log
    assert "MERGE (n:MdraNode" in q1 and len(p1["rows"]) == 8 and "MERGE (a)-[e:REL" in q2 and len(p2["rows"]) == 11
    assert not driver.closed  # injected drivers are owned by the caller


def test_neo4j_status_and_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _row()["state"] == "off"
    with pytest.raises(RuntimeError, match="not configured"):
        sync_neo4j(build_graph([]))
    monkeypatch.setenv("MDRA_NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.setenv("MDRA_NEO4J_PASSWORD", "hunter2pw")
    row = _row()
    assert row["configured"] and "localhost:7687" in row["detail"] and "hunter2pw" not in repr(row)


def test_graph_inputs_from_dashboard_runs() -> None:
    from tests.unit.test_dashboard import _run

    g = core.knowledge_graph([_run("a1", cites=2), _run("b2", status="failed")])
    assert g.counts()["report"] == 1 and g.counts()["source"] == 2


def test_knowledge_page_graph_tab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    testing = pytest.importorskip("streamlit.testing.v1")
    from tests.unit.test_dashboard import _run

    monkeypatch.setenv("DEEP_RESEARCH_DASHBOARD_DIR", str(tmp_path))
    monkeypatch.setenv("DEEP_RESEARCH_CONTROL_URL", "http://127.0.0.1:9")
    RunStore(tmp_path).save(_run("abc123", cites=2))
    at = testing.AppTest.from_file(str(ROOT / "src/deep_research/dashboard/app.py"), default_timeout=30)
    at.query_params["page"] = "knowledge"
    at.run()
    assert not at.exception
    assert [t.label for t in at.tabs][-1] == "Graph"
    captions = " ".join(c.value for c in at.caption)
    assert "1 reports" in captions and "2 sources" in captions and "Neo4j sync: not configured" in captions
