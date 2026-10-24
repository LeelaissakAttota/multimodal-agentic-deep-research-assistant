from deep_research.core.agents.report_agent import _apply_live_findings
from deep_research.dashboard.core import claims
from deep_research.evidence.scoring import claim_scores, score_citations
from deep_research.export import export_report

CITES = [{"number": 1, "url": "https://www.noaa.gov/a", "title": "NOAA"},
         {"number": 2, "url": "https://www.nature.com/b", "title": "Nature"},
         {"number": 3, "url": "https://x.blogspot.com/c", "title": "Blog"}]
CONTENT = """## Findings

Reefs bleach above 1C of warming [1, 2]. Some divers say colour may return quickly [3]. Uncited aside.

| Year | Event |
|---|---|
| 2024 | Fourth global bleaching [1-2] |

- **Bullet** fact [2]

## Sources

[1] NOAA - https://www.noaa.gov/a
"""


def test_scores_cited_statements_only() -> None:
    score_citations(CITES)
    out = claim_scores(CONTENT, CITES)
    texts = [c["text"] for c in out]
    assert len(out) == 4 and not any("Uncited" in t or "NOAA -" in t for t in texts)
    strong, weak, row, bullet = out
    assert strong["citations"] == [1, 2] and strong["label"] == "High" and strong["section"] == "Findings"
    assert weak["label"] == "Low" and "hedged" in weak["reason"]
    assert row["text"].startswith("2024 | Fourth") and row["citations"] == [1, 2]
    assert bullet["text"] == "Bullet fact [2]" and 0.55 <= bullet["confidence"] < strong["confidence"]
    assert claim_scores("Text [9].", CITES) == []


def test_report_agent_and_viewer_and_export() -> None:
    report: dict = {}
    _apply_live_findings(report, [{"kind": "web", "section": "S", "summary": "Reefs bleach [1]. Corals may recover [2].",
                                   "sources": [{"url": "https://www.noaa.gov/a", "title": "NOAA"},
                                               {"url": "https://someone.medium.com/p", "title": "Post"}]}])
    assert [c["citations"] for c in report["claim_scores"]] == [[1], [2]]
    rows = claims(report)
    assert rows[0]["text"] == "Reefs bleach." and rows[0]["label"] and rows[0]["citations"] == [1]
    saved = {k: v for k, v in report.items() if k != "claim_scores"}
    assert claims(saved)[0]["confidence"] == rows[0]["confidence"]
    md = export_report(saved, "md").decode()
    assert "Reefs bleach" in md and "%" in md
