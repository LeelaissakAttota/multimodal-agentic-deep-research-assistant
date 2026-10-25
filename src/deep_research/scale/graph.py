"""Knowledge graph from report claims and cited sources.

Built locally in plain Python (``networkx`` is used for analysis when the ``graph`` extra is installed).
Optionally synced to Neo4j when ``MDRA_NEO4J_URI`` is set. Nodes: ``report`` (one per run), ``claim``,
``source``, ``domain``, ``entity``. Edges: report -makes-> claim, claim -supported_by-> source,
report -cites-> source, source -hosted_on-> domain, claim -mentions-> entity, claim -contradicts-> claim.
Claims are merged across runs by normalized text, sources by URL, entities by normalized name.
Entities are extracted deterministically (acronyms and capitalised phrases), no model is called.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

KINDS = ("report", "claim", "source", "domain", "entity")
_STYLE = {"report": ("box", "#4f7cff"), "claim": ("ellipse", "#f5b94a"), "source": ("note", "#36c48f"),
          "domain": ("hexagon", "#9b8cff"), "entity": ("diamond", "#ff8fa3")}
_ENTITY = re.compile(r"\b(?:[A-Z]{2,6}[0-9]*s?|[A-Z][a-z0-9]+(?:[ -](?:of |de |the )?[A-Z][A-Za-z0-9]+)+)\b")
_NOT_ENTITY = frozenset("The This That These Those It In On At For And But Or If When While As By To From With Of A An "
                        "However Moreover According Overall Most Some Many More".split())


def extract_entities(text: str, limit: int = 8) -> list[str]:
    """Acronyms (``GPT``, ``EU``) and multi-word capitalised names (``European Union``); stable order, deduped."""
    out: list[str] = []
    seen: set[str] = set()
    for m in _ENTITY.finditer(re.sub(r"\[[\d,\s\u2013-]+\]", " ", str(text))):
        words = m.group(0).split()
        while words and words[0] in _NOT_ENTITY:
            words = words[1:]
        name = " ".join(words)
        if len(name) < 2 or (len(words) == 1 and not name.isupper() and not any(ch.isdigit() for ch in name)):
            continue
        k = name.lower()
        if k not in seen:
            seen.add(k)
            out.append(name)
        if len(out) >= limit:
            break
    return out


@dataclass
class KnowledgeGraph:
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: set[tuple[str, str, str]] = field(default_factory=set)

    def add_node(self, node_id: str, kind: str, label: str, **attrs: Any) -> str:
        node = self.nodes.setdefault(node_id, {"kind": kind, "label": label[:160], "runs": set()})
        node.update({k: v for k, v in attrs.items() if k != "run_id" and v is not None})
        if attrs.get("run_id"):
            node["runs"].add(str(attrs["run_id"]))
        return node_id

    def add_edge(self, src: str, dst: str, rel: str) -> None:
        self.edges.add((src, dst, rel))

    def counts(self) -> dict[str, int]:
        c = Counter(n["kind"] for n in self.nodes.values())
        return {**{k: c.get(k, 0) for k in KINDS}, "edges": len(self.edges)}


def claim_id(text: str) -> str:
    return f"claim:{_key(text)}"


def _key(text: str) -> str:
    norm = re.sub(r"\W+", " ", text.lower()).strip()
    return hashlib.sha1(norm.encode()).hexdigest()[:16]


def domain_of(url: Any) -> str:
    try:
        host = urlsplit(str(url or "")).hostname or ""
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def clean_url(url: Any) -> str:
    """Drop credentials, query and fragment (may carry tokens) from a source URL."""
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}{parts.path}"


def build_graph(reports: Iterable[Mapping[str, Any]]) -> KnowledgeGraph:
    """``reports`` = ``[{run_id, title, claims: [{text, citations: [n], label}], citations: [{number, title, url}],
    contradictions: [{claims: [a, b]}]}]`` (``contradictions`` optional)."""
    graph = KnowledgeGraph()
    for rep in reports:
        run_id = str(rep.get("run_id") or "")
        rid = graph.add_node(f"report:{run_id}", "report", str(rep.get("title") or "Report"), run_id=run_id)
        by_number: dict[Any, str] = {}
        for cit in rep.get("citations") or []:
            url = clean_url(cit.get("url"))
            title = str(cit.get("title") or url or "Untitled source")
            sid = graph.add_node(f"source:{url or _key(title)}", "source", title, url=url or None, run_id=run_id)
            by_number[cit.get("number")] = sid
            graph.add_edge(rid, sid, "cites")
            if domain := domain_of(url):
                graph.add_edge(sid, graph.add_node(f"domain:{domain}", "domain", domain, run_id=run_id), "hosted_on")
        for claim in rep.get("claims") or []:
            text = str(claim.get("text") or "").strip()
            if not text:
                continue
            cid = graph.add_node(f"claim:{_key(text)}", "claim", text, verdict=claim.get("label") or None, run_id=run_id)
            graph.add_edge(rid, cid, "makes")
            for number in claim.get("citations") or []:
                if number in by_number:
                    graph.add_edge(cid, by_number[number], "supported_by")
            for name in extract_entities(text):
                graph.add_edge(cid, graph.add_node(f"entity:{_key(name)}", "entity", name, run_id=run_id), "mentions")
        _add_contradictions(graph, run_id, rep.get("contradictions") or [])
    return graph


def _add_contradictions(graph: KnowledgeGraph, run_id: str, items: Iterable[Any]) -> None:
    for item in items:
        pair = [str(t or "").strip() for t in (item.get("claims") or [])] if isinstance(item, Mapping) else []
        if len(pair) == 2 and all(pair) and claim_id(pair[0]) != claim_id(pair[1]):
            a = graph.add_node(claim_id(pair[0]), "claim", pair[0], run_id=run_id)
            b = graph.add_node(claim_id(pair[1]), "claim", pair[1], run_id=run_id)
            graph.add_edge(a, b, "contradicts")


def to_networkx(graph: KnowledgeGraph) -> Any:
    """A ``networkx.MultiDiGraph`` copy (needs the ``graph`` extra)."""
    import networkx as nx

    g = nx.MultiDiGraph()
    for node_id, attrs in graph.nodes.items():
        g.add_node(node_id, **{**attrs, "runs": sorted(attrs["runs"])})
    for src, dst, rel in graph.edges:
        g.add_edge(src, dst, key=rel, rel=rel)
    return g


def top_sources(graph: KnowledgeGraph, limit: int = 5) -> list[tuple[str, int]]:
    """Sources supporting the most claims (networkx in-degree when installed, same result without it)."""
    support = Counter(dst for _, dst, rel in graph.edges if rel == "supported_by")
    try:
        import networkx as nx  # noqa: F401
    except ImportError:
        ranked = support.most_common()
    else:
        g = to_networkx(graph)
        ranked = sorted(((n, sum(1 for _, _, k in g.in_edges(n, keys=True) if k == "supported_by")) for n in g
                         if graph.nodes[n]["kind"] == "source"), key=lambda x: (-x[1], x[0]))
    return [(graph.nodes[n]["label"], c) for n, c in ranked if c][:limit]


def _dot_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def to_dot(graph: KnowledgeGraph, *, max_nodes: int = 80) -> str:
    """Graphviz DOT for ``st.graphviz_chart``; keeps the most connected nodes when the graph is large."""
    degree = Counter([s for s, _, _ in graph.edges] + [d for _, d, _ in graph.edges])
    keep = set(sorted(graph.nodes, key=lambda n: (-degree[n], n))[:max_nodes])
    ids = {n: f"n{i}" for i, n in enumerate(sorted(keep))}
    lines = ["digraph KG {", '  graph [rankdir=LR, bgcolor="transparent"];',
             '  node [style=filled, fontname="Helvetica", fontsize=10, fontcolor="#0b1020"];',
             '  edge [color="#8892b0", fontsize=8, fontcolor="#8892b0"];']
    for node_id in sorted(keep):
        node = graph.nodes[node_id]
        shape, color = _STYLE.get(node["kind"], ("ellipse", "#cccccc"))
        label = node["label"] if len(node["label"]) <= 48 else node["label"][:47] + "…"
        lines.append(f'  {ids[node_id]} [label="{_dot_escape(label)}", shape={shape}, fillcolor="{color}"];')
    for src, dst, rel in sorted(graph.edges):
        if src in keep and dst in keep:
            lines.append(f'  {ids[src]} -> {ids[dst]} [label="{rel}"];')
    lines.append("}")
    return "\n".join(lines)


def sync_neo4j(graph: KnowledgeGraph, driver: Any = None, *, database: str | None = None) -> dict[str, int]:
    """MERGE the graph into Neo4j (idempotent). ``driver`` is injectable; otherwise built from ``MDRA_NEO4J_*``."""
    own = driver is None
    if own:
        driver = neo4j_driver()
    nodes = [{"id": n, "kind": a["kind"], "label": a["label"], "url": a.get("url") or "",
              "runs": sorted(a["runs"])} for n, a in graph.nodes.items()]
    edges = [{"src": s, "dst": d, "rel": r} for s, d, r in sorted(graph.edges)]
    try:
        with driver.session(database=database) as session:
            session.run("CREATE CONSTRAINT mdra_node_id IF NOT EXISTS FOR (n:MdraNode) REQUIRE n.id IS UNIQUE")
            # runs are unioned so syncing one run never drops the others from a shared claim/source
            session.run("UNWIND $rows AS r MERGE (n:MdraNode {id: r.id}) "
                        "SET n.kind = r.kind, n.label = r.label, n.url = r.url, "
                        "n.runs = [x IN coalesce(n.runs, []) WHERE NOT x IN r.runs] + r.runs", rows=nodes)
            session.run("UNWIND $rows AS r MATCH (a:MdraNode {id: r.src}), (b:MdraNode {id: r.dst}) "
                        "MERGE (a)-[e:REL {type: r.rel}]->(b)", rows=edges)
    finally:
        if own:
            driver.close()
    return {"nodes": len(nodes), "edges": len(edges)}


def neo4j_driver(timeout: float = 10.0) -> Any:
    from deep_research.scale.config import ScaleSettings

    s = ScaleSettings()
    if not s.neo4j_uri:
        raise RuntimeError("Neo4j is not configured (set the NEO4J_URI scale setting)")
    try:
        from neo4j import GraphDatabase
    except ImportError as exc:
        raise RuntimeError("install the 'graph' extra for Neo4j support") from exc
    return GraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_user, s.secret("neo4j_password")) if s.neo4j_user else None,
                                connection_timeout=timeout, connection_acquisition_timeout=timeout)
