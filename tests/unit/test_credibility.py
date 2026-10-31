from deep_research.core.agents.report_agent import _apply_live_findings
from deep_research.dashboard.core import citations
from deep_research.evidence.scoring import credibility, label_for


def test_domain_tiers() -> None:
    cases = {
        "https://www.nasa.gov/x": "High", "https://data.gov.uk/a": "High", "https://mit.edu/p": "High",
        "https://www.iitb.ac.in/x": "High", "https://www.who.int/news": "High", "https://www.nature.com/articles/1": "High",
        "https://www.reuters.com/world": "High", "https://en.wikipedia.org/wiki/Coral": "Medium",
        "https://arxiv.org/abs/1": "Medium", "https://someone.medium.com/post": "Low", "https://www.reddit.com/r/x": "Low",
        "https://unknown-shop.biz/a": "Unverified", "https://10.0.0.1/a": "Low",
    }
    for url, label in cases.items():
        assert credibility(url)["label"] == label, url
    assert credibility(None, kind="attachment")["label"] == "Provided"
    assert credibility("http://www.nasa.gov/")["score"] < credibility("https://www.nasa.gov/")["score"]
    assert "not HTTPS" in credibility("http://www.nasa.gov/")["reason"]
    assert "journal" in credibility("https://www.nature.com/a")["reason"]
    assert label_for(0.5) == "Unverified" and label_for(0.2) == "Low"


def test_report_citations_carry_credibility_and_dashboard_falls_back() -> None:
    report: dict = {}
    _apply_live_findings(report, [{"kind": "web", "section": "S", "summary": "Reefs bleach [1].",
                                   "sources": [{"url": "https://www.noaa.gov/reefs", "title": "NOAA"}]}])
    assert report["citations"][0]["credibility"]["label"] == "High"
    saved = {"citations": [{"number": 1, "title": "Blog", "url": "https://x.blogspot.com/a"}]}
    assert citations(saved)[0]["credibility"]["label"] == "Low"
