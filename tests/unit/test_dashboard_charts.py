"""Phase 4: report charts and fact-check verdicts in the dashboard viewer. No network."""

from __future__ import annotations

from pathlib import Path

import pytest

from deep_research.dashboard.core import RunStore, report_charts, strip_section, verification_rows

ROOT = Path(__file__).resolve().parents[2]
REPORT = {
    "content": "## A\n\nX grew [1].\n\n## Charts\n\n```text\nSupported █ 1\n```\n\n## Sources\n\n[1] S - https://a.org",
    "citations": [{"number": 1, "title": "S", "url": "https://a.org"}],
    "charts": [{"id": "verification", "title": "Claim verification verdicts", "unit": "claims",
                "data": [{"label": "Supported", "value": 1}, {"label": "bad", "value": "x"}]}, {"id": "empty", "data": []}, "junk"],
    "verification": {"support_score": 0.75, "methods": ["heuristic"],
                     "claims": [{"claim": "X grew", "citations": [1], "verdict": "supported", "method": "heuristic"},
                                {"claim": "Y fell", "citations": [1], "verdict": "contradicted", "method": "llm:m:free"}]},
    "contradictions": [{"kind": "claim_vs_source", "claims": ["Y fell"], "reason": "r"}],
}


def test_report_charts_rows_skip_bad_data() -> None:
    assert report_charts(REPORT) == [{"id": "verification", "title": "Claim verification verdicts", "unit": "claims",
                                      "labels": ["Supported"], "values": [1.0]}]
    assert report_charts({}) == []


def test_verification_rows_badges_and_methods() -> None:
    ver = verification_rows(REPORT)
    assert ver["score"] == "75%" and len(ver["contradictions"]) == 1
    assert [(r["label"], r["css"], r["method"]) for r in ver["rows"]] == [
        ("Supported", "b-ok", "offline heuristic"), ("Contradicted", "b-bad", "LLM judge")]
    assert verification_rows({})["rows"] == []


def test_strip_section_removes_only_charts() -> None:
    out = strip_section(REPORT["content"], "Charts")
    assert "## Charts" not in out and "## Sources" in out and "X grew" in out


def test_report_view_renders_charts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    testing = pytest.importorskip("streamlit.testing.v1")
    monkeypatch.setenv("DEEP_RESEARCH_DASHBOARD_DIR", str(tmp_path))
    monkeypatch.setenv("DEEP_RESEARCH_CONTROL_URL", "http://127.0.0.1:9")
    RunStore(tmp_path).save({"run_id": "r1", "status": "completed", "created_at": "2026-10-24T10:00:00+00:00",
                             "params": {"objective": "Q", "mode": "live"}, "result": {"report": REPORT}})
    at = testing.AppTest.from_file(str(ROOT / "src/deep_research/dashboard/app.py"), default_timeout=30)
    at.query_params.update({"page": "researches", "run": "r1", "view": "report"})
    at.run()
    assert not at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "Claim verification verdicts" in text and "Fact check" in text and "Contradicted" in text
    assert "Supported █" not in text  # text chart replaced by the native chart
    assert len(at.get("vega_lite_chart")) >= 1


def _verified_run(rid: str) -> dict:
    report = {**REPORT, "verification": {**REPORT["verification"], "total": 2,
                                         "counts": {"supported": 1, "contradicted": 1}}}
    return {"run_id": rid, "status": "completed", "created_at": "2026-10-24T10:00:00+00:00",
            "params": {"objective": "Q", "mode": "live"}, "result": {"report": report}}


def test_knowledge_and_sources_carry_verification() -> None:
    from deep_research.dashboard.core import knowledge, source_rows, verdict_label, verification_line

    kb = knowledge([_verified_run("a"), _verified_run("b")])
    assert kb["reports"][0]["verification"]["label"] == "1✓ 1✗"
    agg = kb["verification"]
    assert agg["reports"] == 2 and agg["score"] == 0.75 and agg["contradictions"] == 2
    assert "2 fact-checked report(s)" in verification_line(agg) and "75%" in verification_line(agg)
    assert "No fact-checked" in verification_line(knowledge([])["verification"])
    rows = source_rows([_verified_run("a")])
    assert verdict_label(rows[0]["verdicts"]) == "1✓ 1✗"
    assert verdict_label(None) == ""


def test_sources_and_knowledge_pages_show_fact_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    testing = pytest.importorskip("streamlit.testing.v1")
    monkeypatch.setenv("DEEP_RESEARCH_DASHBOARD_DIR", str(tmp_path))
    monkeypatch.setenv("DEEP_RESEARCH_CONTROL_URL", "http://127.0.0.1:9")
    RunStore(tmp_path).save(_verified_run("r1"))
    for page in ("sources", "knowledge"):
        at = testing.AppTest.from_file(str(ROOT / "src/deep_research/dashboard/app.py"), default_timeout=30)
        at.query_params["page"] = page
        at.run()
        assert not at.exception
        text = " ".join(m.value for m in at.markdown)
        assert "Fact check" in text and "mean support 75%" in text, page
    assert "fact check 75% 1✓ 1✗" in text
