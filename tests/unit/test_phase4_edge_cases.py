"""Edge cases for Phase 4: fact verification, contradictions, charts, offline eval."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import deep_research.verification.charts as ch
from deep_research.evaluation.__main__ import main as eval_main
from deep_research.evaluation.offline_eval import case_from_run, run_suite, score_report, scorecard_table
from deep_research.verification.contradictions import contradictions_markdown, find_contradictions
from deep_research.verification.fact_check import (
    ClaimVerifier,
    ExtractedClaim,
    extract_claims,
    heuristic_verdict,
    verification_markdown,
)


def test_extract_claims_empty_and_uncited() -> None:
    assert extract_claims("") == []
    assert extract_claims("# Title\n\nA long sentence without any citation at all.") == []


def test_extract_claims_unicode() -> None:
    claims = extract_claims("## Résumé\n\nLe café coûte 3 € à Zürich — très cher [1].")
    assert len(claims) == 1 and claims[0].citations == [1]


def test_heuristic_no_evidence_and_blank() -> None:
    assert heuristic_verdict("Solar is cheap now", [])[0] == "no_evidence"
    assert heuristic_verdict("Solar is cheap now", ["   "])[0] == "no_evidence"


def test_heuristic_negation_flip() -> None:
    v, _, _ = heuristic_verdict("Solar panels are not efficient in winter climates",
                                ["Solar panels are efficient in winter climates."])
    assert v == "contradicted"


class _Gateway:
    pass


def _claims() -> list[ExtractedClaim]:
    return [ExtractedClaim(text="Solar capacity grew 20% in 2023", citations=[1], section="S")]


def test_verifier_no_gateway_uses_heuristic() -> None:
    res = asyncio.run(ClaimVerifier().verify(_claims(), {1: "Solar capacity grew 20% in 2023."}))
    assert res["claims"][0]["method"] == "heuristic"


@pytest.mark.parametrize("reply", [{}, {"verdict": "maybe"}, {"verdict": 7}])
def test_verifier_malformed_judge_falls_back(monkeypatch: pytest.MonkeyPatch, reply: dict[str, Any]) -> None:
    import deep_research.core.agents.llm_support as ls

    async def fake(*a: Any, **k: Any) -> tuple[dict[str, Any], str]:
        return reply, "m"

    monkeypatch.setattr(ls, "ask_json", fake)
    res = asyncio.run(ClaimVerifier(_Gateway()).verify(_claims(), {1: "Solar capacity grew 20% in 2023."}))
    row = res["claims"][0]
    assert row["method"] == "heuristic" and row.get("fallback_reason") == "invalid_verdict"


def test_verifier_judge_raises_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    import deep_research.core.agents.llm_support as ls

    async def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("down")

    monkeypatch.setattr(ls, "ask_json", boom)
    res = asyncio.run(ClaimVerifier(_Gateway()).verify(_claims(), {1: "Solar capacity grew 20% in 2023."}))
    assert res["claims"][0]["fallback_reason"] == "RuntimeError"


def test_verifier_bad_confidence_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    import deep_research.core.agents.llm_support as ls

    async def fake(*a: Any, **k: Any) -> tuple[dict[str, Any], str]:
        return {"verdict": "Supported", "confidence": "high"}, "m"

    monkeypatch.setattr(ls, "ask_json", fake)
    res = asyncio.run(ClaimVerifier(_Gateway()).verify(_claims(), {1: "x"}))
    assert res["claims"][0]["verdict"] == "supported" and res["claims"][0]["confidence"] == 0.5


def test_verification_markdown_empty() -> None:
    res = asyncio.run(ClaimVerifier().verify([], {}))
    assert isinstance(verification_markdown(res), str)


def test_contradictions_negation_and_empty() -> None:
    a = ExtractedClaim(text="Nuclear power is safe for nearby residents", citations=[1], section="S")
    b = ExtractedClaim(text="Nuclear power is not safe for nearby residents", citations=[2], section="S")
    found = find_contradictions([a, b])
    assert found and found[0]["kind"] == "cross_source"
    assert find_contradictions([]) == []
    assert "No contradictions" in contradictions_markdown([], claim_count=0)


def test_contradictions_same_source_ignored() -> None:
    a = ExtractedClaim(text="Nuclear power is safe for nearby residents", citations=[1], section="S")
    b = ExtractedClaim(text="Nuclear power is not safe for nearby residents", citations=[1], section="S")
    assert find_contradictions([a, b]) == []


def test_charts_empty_and_single() -> None:
    assert ch.build_charts({}) == []
    assert ch.charts_markdown([]) == ""
    one = ch.build_charts({"citations": [{"url": "https://www.example.com/a"}]})
    assert one[0]["data"] == [{"label": "example.com", "value": 1.0}]
    md = ch.charts_markdown(list[Any](one))  # charts.py:65 list[Mapping] param rejects build_charts output
    assert "example.com" in md and "█" in md
    assert ch.charts_markdown([{"title": "t", "data": []}]) == "## Charts"


def test_charts_all_zero_values() -> None:
    md = ch.charts_markdown([{"title": "t", "unit": "u", "data": [{"label": "a", "value": 0}]}])
    assert "a" in md


def test_score_report_empty() -> None:
    card = score_report("obj", "", {})
    assert card.claims == 0
    assert "report" in scorecard_table([card])


def test_case_from_run_variants() -> None:
    assert case_from_run({}) is None
    assert case_from_run({"result": {"report": "text"}}) is None
    case = case_from_run({"params": None, "report": {"content": "x", "citations": [{"number": "1"}]}})
    assert case is not None and case.excerpts == {}


def test_eval_cli_no_args(capsys: pytest.CaptureFixture[str]) -> None:
    assert eval_main([]) == 0
    assert capsys.readouterr().out.strip()


def test_eval_cli_invalid_flag() -> None:
    with pytest.raises(SystemExit) as exc:
        eval_main(["--min-overall", "abc"])
    assert exc.value.code == 2


def test_eval_cli_skip_and_threshold(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"x": 1}), encoding="utf-8")
    assert eval_main([str(p), "--json"]) == 0
    out = capsys.readouterr()
    assert "skip r.json" in out.err and json.loads(out.out) == []
    assert eval_main(["--min-overall", "1.01"]) == 1


def test_run_suite_empty_list() -> None:
    assert run_suite([]) == []


def test_score_report_inside_running_loop_works() -> None:
    """Former gap (asyncio.run inside a running loop) is fixed: score_report offloads to a worker thread."""

    async def inner() -> int:
        return score_report("o", "Solar capacity grew 20% in 2023 [1].", {1: "Solar capacity grew 20%."}).claims

    assert asyncio.run(inner()) == 1
