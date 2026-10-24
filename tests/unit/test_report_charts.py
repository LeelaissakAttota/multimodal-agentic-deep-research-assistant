"""Phase 4: charts in reports. No network."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from deep_research.core.agents.report_agent import DeterministicReportAgent
from deep_research.domain.research.research_state import ResearchState
from deep_research.verification.charts import build_charts, charts_markdown


def test_build_charts_from_report() -> None:
    report = {
        "content": "## Fundamentals\n\nA [1, 2]. B [3].\n\n## Risks\n\nC [2].\n\n## Sources\n\n[1] x - https://a.org",
        "citations": [{"url": "https://www.a.org/x"}, {"url": "https://a.org/y"}, {"url": None, "kind": "attachment"}],
        "verification": {"total": 3, "counts": {"supported": 2, "contradicted": 1, "unsupported": 0}},
    }
    charts = {c["id"]: c for c in build_charts(report)}
    assert charts["verification"]["data"] == [{"label": "Supported", "value": 2.0}, {"label": "Contradicted", "value": 1.0}]
    assert charts["section_citations"]["data"] == [{"label": "Fundamentals", "value": 3}, {"label": "Risks", "value": 1}]
    assert charts["source_domains"]["data"][0] == {"label": "a.org", "value": 2}
    md = charts_markdown(list(charts.values()))
    assert md.startswith("## Charts") and "```text" in md and "█" in md


def test_no_charts_for_empty_report() -> None:
    assert build_charts({"content": "", "citations": []}) == []
    assert charts_markdown([]) == ""


def test_report_agent_embeds_charts() -> None:
    state = ResearchState(request_id=uuid4())
    state.metadata["live_findings"] = [{"kind": "web", "section": "Overview", "summary": "Solar output grew strongly [1].",
                                        "sources": [{"source_id": "a", "url": "https://iea.org/solar", "title": "IEA",
                                                     "excerpt": "Solar output grew strongly last year."}]}]
    report = asyncio.run(DeterministicReportAgent().generate_report(state, {}))
    assert {c["id"] for c in report["charts"]} == {"verification", "section_citations", "source_domains"}
    assert report["content"].index("## Charts") < report["content"].index("## Sources")
