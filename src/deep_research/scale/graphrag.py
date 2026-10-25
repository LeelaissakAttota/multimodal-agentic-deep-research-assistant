"""GraphRAG: Neo4j sync of the knowledge graph and graph-augmented retrieval for the report chat.

Off by default. ``MDRA_GRAPH_RAG=true`` expands the hybrid RAG hits of the follow-up chat through graph
neighbours: claims that share a cited source, claims that mention the same entity, and claims that
contradict a hit. Neighbours are merged into the ranking at ``MDRA_GRAPH_WEIGHT`` x the seed's score and keep
their own report citations; only claims of the report being asked about are ever added, so every citation
stays valid. The neighbours come from Neo4j when ``MDRA_NEO4J_URI`` is set and reachable, otherwise from the
local in-process graph (:mod:`deep_research.scale.graph`, the same graph the Knowledge Base draws).

``MDRA_GRAPH_AUTO_SYNC=true`` MERGEs each finished run into Neo4j; ``deep-research graph-sync`` syncs every
saved run. Both are idempotent. Errors are logged by exception type only (never the URI or credentials).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from deep_research.scale.graph import KnowledgeGraph, build_graph, claim_id, clean_url

log = logging.getLogger(__name__)
REL_FACTOR = {"contradicts": 1.0, "shared_source": 0.8, "shared_entity": 0.6}


class GraphSettings(BaseSettings):
    rag: bool = Field(default=False, description="expand report-chat RAG hits through knowledge-graph neighbours")
    auto_sync: bool = Field(default=False, description="MERGE every finished run into Neo4j (needs MDRA_NEO4J_URI)")
    backend: Literal["auto", "neo4j", "local"] = Field(default="auto", description="auto = Neo4j when configured "
                                                       "and reachable, else the local graph")
    weight: float = Field(default=0.5, ge=0.0, le=1.0, description="neighbour score = weight x seed score x relation")
    max_neighbours: int = Field(default=4, ge=0, le=20)
    timeout_seconds: float = Field(default=5.0, gt=0, le=60)

    model_config = SettingsConfigDict(
        env_file=os.environ.get("DEEP_RESEARCH_ENV_FILE") or ".env",
        env_file_encoding="utf-8",
        env_prefix="MDRA_GRAPH_",
        case_sensitive=False,
        extra="ignore",
    )


@dataclass(frozen=True)
class Neighbour:
    claim_id: str
    relation: str  # contradicts | shared_source | shared_entity
    seed: str


class GraphBackend(Protocol):
    name: str

    def neighbours(self, run_id: str, seeds: list[str], limit: int) -> list[Neighbour]: ...


# ----- report -> graph input ---------------------------------------------------------------------------
def report_input(run_id: str, report: Mapping[str, Any], title: str = "") -> dict[str, Any]:
    """Graph input for one report, using the raw ``claim_scores`` text (the same text the RAG chunks carry)."""
    claims = [{"text": str(c["text"]), "citations": list(c.get("citations") or []), "label": c.get("label")}
              for c in report.get("claim_scores") or [] if isinstance(c, Mapping) and c.get("text")]
    cits = [{"number": c.get("number"), "title": c.get("title"), "url": c.get("url")}
            for c in report.get("citations") or [] if isinstance(c, Mapping)]
    conflicts = [c for c in report.get("contradictions") or [] if isinstance(c, Mapping)]
    return {"run_id": run_id, "title": title or str(report.get("title") or "Report"), "claims": claims,
            "citations": cits, "contradictions": conflicts}


def source_ids(report: Mapping[str, Any], numbers: Iterable[int]) -> list[str]:
    """Graph ids of the report's sources with these citation numbers (same keys as :func:`build_graph`)."""
    want = set(numbers)
    out = []
    for c in report.get("citations") or []:
        if isinstance(c, Mapping) and c.get("number") in want:
            url = clean_url(c.get("url"))
            if url:
                out.append(f"source:{url}")
    return out


# ----- backends ----------------------------------------------------------------------------------------
class LocalGraphBackend:
    """Neighbour lookup on the in-process :class:`KnowledgeGraph` (no service needed)."""

    name = "local"

    def __init__(self, graph: KnowledgeGraph) -> None:
        self.graph = graph
        self._out: dict[str, set[tuple[str, str]]] = {}
        self._in: dict[str, set[tuple[str, str]]] = {}
        for s, d, r in graph.edges:
            self._out.setdefault(s, set()).add((d, r))
            self._in.setdefault(d, set()).add((s, r))

    def _claim_in_run(self, node: str, run_id: str) -> bool:
        n = self.graph.nodes.get(node)
        return bool(n and n["kind"] == "claim" and run_id in n["runs"])

    def neighbours(self, run_id: str, seeds: list[str], limit: int) -> list[Neighbour]:
        out: list[Neighbour] = []
        for seed in seeds:
            found: list[tuple[str, str]] = []
            if seed.startswith("claim:"):
                for other, rel in sorted(self._out.get(seed, set()) | self._in.get(seed, set())):
                    if rel == "contradicts":
                        found.append((other, "contradicts"))
                for mid, rel in sorted(self._out.get(seed, set())):
                    if rel in ("supported_by", "mentions"):
                        kind = "shared_source" if rel == "supported_by" else "shared_entity"
                        found += [(c, kind) for c, r2 in sorted(self._in.get(mid, set())) if r2 == rel]
            elif seed.startswith("source:"):
                found += [(c, "shared_source") for c, r in sorted(self._in.get(seed, set())) if r == "supported_by"]
            out += [Neighbour(c, rel, seed) for c, rel in found if c != seed and self._claim_in_run(c, run_id)]
        return _dedupe(out)[:limit]


_NEIGHBOUR_CYPHER = """
UNWIND $seeds AS sid
MATCH (s:MdraNode {id: sid})
CALL (s) {
  MATCH (s)-[:REL {type: 'contradicts'}]-(c:MdraNode {kind: 'claim'}) RETURN c, 'contradicts' AS rel, 0 AS o
  UNION
  MATCH (s)-[:REL {type: 'supported_by'}]->(:MdraNode)<-[:REL {type: 'supported_by'}]-(c:MdraNode {kind: 'claim'})
  RETURN c, 'shared_source' AS rel, 1 AS o
  UNION
  MATCH (s)<-[:REL {type: 'supported_by'}]-(c:MdraNode {kind: 'claim'}) WHERE s.kind = 'source'
  RETURN c, 'shared_source' AS rel, 1 AS o
  UNION
  MATCH (s)-[:REL {type: 'mentions'}]->(:MdraNode)<-[:REL {type: 'mentions'}]-(c:MdraNode {kind: 'claim'})
  RETURN c, 'shared_entity' AS rel, 2 AS o
}
WITH sid, c, rel, o WHERE c.id <> sid AND $run IN c.runs
RETURN sid, c.id AS id, rel ORDER BY o, id LIMIT $limit
"""


class Neo4jGraphBackend:
    """Neighbour lookup with one Cypher query against the synced graph."""

    name = "neo4j"

    def __init__(self, driver: Any, database: str | None = None) -> None:
        self.driver = driver
        self.database = database

    def neighbours(self, run_id: str, seeds: list[str], limit: int) -> list[Neighbour]:
        with self.driver.session(database=self.database) as session:
            rows = list(session.run(_NEIGHBOUR_CYPHER, seeds=seeds, run=run_id, limit=max(limit * 4, limit)))
        out = []
        for row in rows:
            get = row.get if hasattr(row, "get") else row.__getitem__
            out.append(Neighbour(str(get("id")), str(get("rel")), str(get("sid"))))
        order = {s: i for i, s in enumerate(seeds)}
        out.sort(key=lambda n: (order.get(n.seed, len(seeds)), list(REL_FACTOR).index(n.relation)
                                if n.relation in REL_FACTOR else 9, n.claim_id))
        return _dedupe(out)[:limit]


def _dedupe(items: list[Neighbour]) -> list[Neighbour]:
    seen: set[str] = set()
    out = []
    for n in items:
        if n.claim_id not in seen:
            seen.add(n.claim_id)
            out.append(n)
    return out


def neo4j_configured() -> bool:
    from deep_research.scale.config import ScaleSettings, has_module

    return bool(ScaleSettings().neo4j_uri) and has_module("neo4j")


def open_backend(run_id: str, report: Mapping[str, Any], settings: GraphSettings | None = None,
                 driver: Any = None) -> tuple[GraphBackend, str]:
    """``(backend, note)``: Neo4j when configured and reachable (and this run is synced), else the local graph."""
    settings = settings or GraphSettings()
    note = ""
    if settings.backend != "local" and (driver is not None or neo4j_configured()):
        try:
            if driver is None:
                from deep_research.scale.graph import neo4j_driver

                driver = neo4j_driver(settings.timeout_seconds)
            driver.verify_connectivity()
            if settings.auto_sync:
                sync_run(run_id, report, driver=driver)
            return Neo4jGraphBackend(driver), ""
        except Exception as exc:  # noqa: BLE001 - degrade to the local graph; type only
            note = f"neo4j unavailable ({type(exc).__name__}), local graph"
            log.warning("graph rag: %s", note)
    elif settings.backend == "neo4j":
        note = "neo4j not configured, local graph"
    return LocalGraphBackend(build_graph([report_input(run_id, report)])), note


def expand(run_id: str, report: Mapping[str, Any], seeds: list[tuple[float, list[str]]],
           backend: GraphBackend, settings: GraphSettings | None = None) -> list[tuple[float, dict[str, Any], str, str]]:
    """Graph neighbours of the seed hits as ``(score, claim_row, relation, seed id)``; ``seeds`` = ``(score, [node ids])``.

    Only claims of this report with at least one valid citation are returned; scores are
    ``weight x seed score x relation factor``.
    """
    settings = settings or GraphSettings()
    if settings.max_neighbours == 0 or not seeds:
        return []
    valid = {c.get("number") for c in report.get("citations") or [] if isinstance(c, Mapping)}
    claims: dict[str, dict[str, Any]] = {}
    for c in report.get("claim_scores") or []:
        if isinstance(c, Mapping) and c.get("text"):
            cites = sorted({int(n) for n in c.get("citations") or [] if str(n).isdigit() and int(n) in valid})
            if cites:
                claims.setdefault(claim_id(str(c["text"])), {"text": str(c["text"]), "citations": cites})
    score_of: dict[str, float] = {}
    ids: list[str] = []
    for score, nodes in seeds:
        for node in nodes:
            if node not in score_of:
                score_of[node] = score
                ids.append(node)
    seed_claims = {n for n in ids if n.startswith("claim:")}
    found = backend.neighbours(run_id, ids, settings.max_neighbours * 3)
    out = []
    for n in found:
        row = claims.get(n.claim_id)
        if row is None or n.claim_id in seed_claims:
            continue
        out.append((settings.weight * score_of.get(n.seed, 0.0) * REL_FACTOR.get(n.relation, 0.5), row, n.relation,
                    n.seed))
    out.sort(key=lambda x: -x[0])
    return out[:settings.max_neighbours]


# ----- sync --------------------------------------------------------------------------------------------
def sync_runs(runs: Iterable[Mapping[str, Any]], driver: Any = None) -> dict[str, int]:
    """MERGE every saved run (``{run_id, result: {report}}``) into Neo4j."""
    from deep_research.rag.backfill import run_report, run_title
    from deep_research.scale.graph import sync_neo4j

    inputs = []
    for run in runs:
        report = run_report(dict(run))
        if report and run.get("run_id"):
            inputs.append(report_input(str(run["run_id"]), report, run_title(dict(run))))
    res = sync_neo4j(build_graph(inputs), driver)
    return {"runs": len(inputs), **res}


def sync_run(run_id: str, report: Mapping[str, Any], title: str = "", driver: Any = None) -> dict[str, int]:
    from deep_research.scale.graph import sync_neo4j

    return sync_neo4j(build_graph([report_input(run_id, report, title)]), driver)


def auto_sync(run_id: str, result: Any, title: str = "") -> bool:
    """Called when a run finishes: sync its report when ``MDRA_GRAPH_AUTO_SYNC`` and Neo4j are configured.

    Never raises; returns whether a sync happened.
    """
    try:
        if not GraphSettings().auto_sync or not neo4j_configured():
            return False
        report = result.get("report") if isinstance(result, Mapping) else None
        if not isinstance(report, Mapping):
            return False
        sync_run(run_id, report, title)
        return True
    except Exception as exc:  # noqa: BLE001 - the run already finished; type only, never the URI
        log.warning("graph auto-sync failed: %s", type(exc).__name__)
        return False
