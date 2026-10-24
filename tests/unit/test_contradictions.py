"""Phase 4: contradiction detection section in reports. No network."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from deep_research.core.agents.report_agent import DeterministicReportAgent
from deep_research.domain.research.research_state import ResearchState
from deep_research.verification.contradictions import contradictions_markdown, find_contradictions
from deep_research.verification.fact_check import extract_claims


def claims_of(text: str) -> list:
    return extract_claims(text)


def test_cross_source_negation_antonym_and_figures() -> None:
    cl = claims_of("Small modular reactors are safe for urban grids [1]. Small modular reactors are unsafe for urban grids [2]. "
                   "Global SMR capacity will reach 40 GW by 2040 [1]. Global SMR capacity will reach 21 GW by 2040 [3]. "
                   "Wind turbines do not need water cooling [2]. Wind turbines need water cooling [4].")
    reasons = [c["reason"] for c in find_contradictions(cl)]
    assert any("opposite direction" in r for r in reasons)
    assert any("conflicting figures" in r and "gw" in r for r in reasons)
    assert any("negates" in r for r in reasons)


def test_same_source_and_unrelated_claims_ignored() -> None:
    cl = claims_of("Reactors are safe for grids [1]. Reactors are unsafe for grids [1]. Penguins swim in cold oceans [2].")
    assert find_contradictions(cl) == []


def test_verifier_contradictions_included_and_markdown() -> None:
    verification = {"claims": [{"claim": "X is 9 MW", "citations": [1], "verdict": "contradicted", "rationale": "source says 3 MW"}]}
    items = find_contradictions([], verification)
    assert items[0]["kind"] == "claim_vs_source"
    md = contradictions_markdown(items, claim_count=1)
    assert md.startswith("## Contradictions and disagreements") and "source says 3 MW" in md
    assert "No contradictions detected among 4" in contradictions_markdown([], claim_count=4)


def test_report_has_contradiction_section() -> None:
    state = ResearchState(request_id=uuid4())
    state.metadata["live_findings"] = [
        {"kind": "web", "section": "A", "summary": "Electric vehicle sales increase in Europe this year [1].",
         "sources": [{"source_id": "a", "url": "https://a.org", "title": "A"}]},
        {"kind": "web", "section": "B", "summary": "Electric vehicle sales decrease in Europe this year [1].",
         "sources": [{"source_id": "b", "url": "https://b.org", "title": "B"}]},
    ]
    report = asyncio.run(DeterministicReportAgent().generate_report(state, {}))
    assert "## Contradictions and disagreements" in report["content"]
    assert report["contradictions"][0]["citations"] == [[1], [2]]
    assert report["content"].index("Contradictions") < report["content"].index("## Sources")
