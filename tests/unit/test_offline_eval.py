"""Phase 4: offline eval suite (faithfulness / relevance / coverage scorecard). No network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deep_research.evaluation import run_suite, score_report
from deep_research.evaluation.__main__ import main
from deep_research.evaluation.offline_eval import case_from_run, scorecard_table


def test_bundled_suite_ranks_failure_modes() -> None:
    cards = {c.name: c for c in run_suite()}
    good = cards["grounded_complete"]
    assert good.faithfulness == 1.0 and good.coverage == 1.0 and good.overall >= 0.85
    assert cards["hallucinated_figures"].faithfulness < 0.5
    assert cards["off_topic"].relevance == 0.0 and cards["off_topic"].coverage == 0.0
    assert cards["partial_coverage"].coverage < 0.5 and cards["partial_coverage"].faithfulness == 1.0
    assert good.overall == max(c.overall for c in cards.values())
    assert cards["hallucinated_figures"].details["verdicts"]["contradicted"] >= 1


def test_coverage_without_expected_points_uses_sections() -> None:
    card = score_report("solar panel degradation", "## A\n\nSolar panels degrade slowly [1].\n\n## B\n\nNo citations here.",
                        {1: "Solar panels degrade slowly over decades."})
    assert card.coverage == 0.5 and card.details["sections"] == 2


def test_empty_report_scores_zero() -> None:
    card = score_report("anything", "", {})
    assert (card.faithfulness, card.relevance, card.coverage, card.claims) == (0.0, 0.0, 0.0, 0)


def test_table_and_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert "mean" in scorecard_table(run_suite())
    assert main([]) == 0 and "grounded_complete" in capsys.readouterr().out
    assert main(["--json", "--min-overall", "0.99"]) == 1
    assert json.loads(capsys.readouterr().out)[0]["name"] == "grounded_complete"
    run = {"run_id": "r1", "params": {"objective": "solar degradation"},
           "result": {"report": {"content": "## A\n\nSolar panels degrade slowly [1].",
                                 "citations": [{"number": 1, "excerpt": "Solar panels degrade slowly."}]}}}
    path = tmp_path / "run.json"
    path.write_text(json.dumps(run))
    (tmp_path / "bad.json").write_text("{}")
    assert main([str(path), str(tmp_path / "bad.json")]) == 0
    out = capsys.readouterr()
    assert "run" in out.out and "skip bad.json" in out.err
    assert case_from_run({}) is None


def test_report_citations_keep_excerpt_for_later_eval() -> None:
    import asyncio
    from uuid import uuid4

    from deep_research.core.agents.report_agent import DeterministicReportAgent
    from deep_research.domain.research.research_state import ResearchState

    state = ResearchState(request_id=uuid4())
    state.metadata["live_findings"] = [{"kind": "web", "section": "A", "summary": "Solar panels degrade slowly [1].",
                                        "sources": [{"source_id": "s", "url": "https://a.org", "title": "A",
                                                     "excerpt": "Solar panels degrade slowly." + " x" * 1000}]}]
    report = asyncio.run(DeterministicReportAgent().generate_report(state, {}))
    assert report["citations"][0]["excerpt"].startswith("Solar panels") and len(report["citations"][0]["excerpt"]) == 1200
    case = case_from_run({"params": {"objective": "solar panel degradation"}, "result": {"report": report}})
    assert case is not None and run_suite([case])[0].faithfulness == 1.0


def test_score_report_is_safe_inside_running_loop() -> None:
    import asyncio

    from deep_research.evaluation import score_report_async

    args = ("solar panel degradation", "## A\n\nSolar panels degrade slowly [1].", {1: "Solar panels degrade slowly."})

    async def inside() -> tuple[float, float]:
        sync_card = score_report(*args)  # would raise with a bare asyncio.run
        async_card = await score_report_async(*args)
        return sync_card.faithfulness, async_card.overall

    faith, overall = asyncio.run(inside())
    assert faith == 1.0 and overall == score_report(*args).overall
