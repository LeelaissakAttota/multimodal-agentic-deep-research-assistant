"""Report charts: small bar-chart specs derived from a finished report.

Specs are plain JSON (``{"id", "title", "kind": "bar", "unit", "data": [{"label", "value"}]}``)
so they persist with the run and render both as a Markdown text chart in the
report and as native charts in the dashboard viewer. No plotting dependency.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from deep_research.verification.text import citation_numbers

_SKIP = {"sources", "attached files", "claim verification", "contradictions and disagreements", "charts",
         "agent decisions"}
_CITE_GROUP = re.compile(r"\[\d+(?:\s*[,\u2013-]\s*\d+)*\]")
_VERDICT_LABELS = (("supported", "Supported"), ("partially_supported", "Partial"), ("unsupported", "Unsupported"),
                   ("contradicted", "Contradicted"), ("no_evidence", "No evidence"))


def _chart(cid: str, title: str, unit: str, data: list[tuple[str, float]]) -> dict[str, Any]:
    return {"id": cid, "title": title, "kind": "bar", "unit": unit,
            "data": [{"label": label, "value": value} for label, value in data]}


def build_charts(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    charts: list[dict[str, Any]] = []
    verification = report.get("verification")
    if isinstance(verification, Mapping) and verification.get("total"):
        counts = verification.get("counts") or {}
        data = [(label, float(counts.get(key, 0))) for key, label in _VERDICT_LABELS if counts.get(key)]
        charts.append(_chart("verification", "Claim verification verdicts", "claims", data))
    sections = _citations_per_section(str(report.get("content") or ""))
    if sections:
        charts.append(_chart("section_citations", "Citations per section", "citations", sections))
    domains: dict[str, float] = {}
    for c in report.get("citations") or []:
        if isinstance(c, Mapping):
            host = urlparse(str(c.get("url") or "")).netloc.removeprefix("www.") or "attachment"
            domains[host] = domains.get(host, 0) + 1
    if domains:
        top = sorted(domains.items(), key=lambda kv: (-kv[1], kv[0]))[:8]
        charts.append(_chart("source_domains", "Sources by domain", "sources", top))
    return charts


def _citations_per_section(content: str) -> list[tuple[str, float]]:
    out: dict[str, float] = {}
    section: str | None = None
    for line in content.splitlines():
        heading = re.match(r"^#{1,6}\s+(.*)$", line.strip())
        if heading:
            name = heading.group(1).strip()
            section = None if name.lower() in _SKIP else name
            continue
        if section is not None:
            n = sum(len(citation_numbers(m.group(0))) for m in _CITE_GROUP.finditer(line))
            if n:
                out[section] = out.get(section, 0) + n
    return list(out.items())


def charts_markdown(charts: Sequence[Mapping[str, Any]], *, width: int = 24) -> str:
    """Text bar charts in fenced blocks so they render the same in any Markdown viewer and in exports."""
    if not charts:
        return ""
    blocks = ["## Charts"]
    for chart in charts:
        data = [d for d in chart.get("data") or [] if isinstance(d, Mapping)]
        if not data:
            continue
        peak = max(float(d.get("value") or 0) for d in data) or 1.0
        label_w = min(28, max(len(str(d.get("label"))) for d in data))
        rows = []
        for d in data:
            value = float(d.get("value") or 0)
            label = str(d.get("label"))[:label_w].ljust(label_w)
            rows.append(f"{label} {'█' * max(1, round(width * value / peak)) if value else ''} {value:g}")
        blocks.append(f"**{chart.get('title')}** ({chart.get('unit')})\n\n```text\n" + "\n".join(rows) + "\n```")
    return "\n\n".join(blocks)
