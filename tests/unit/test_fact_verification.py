"""Phase 4: per-claim fact verification (free LLM judge + offline heuristic fallback). No network."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from deep_research.core.agents.report_agent import DeterministicReportAgent
from deep_research.domain.research.research_state import ResearchState
from deep_research.errors.base import ModelError
from deep_research.models.model_gateway import ModelGateway, ModelRequest, ModelResponse
from deep_research.verification.fact_check import (
    ClaimVerifier,
    extract_claims,
    heuristic_verdict,
    verification_markdown,
)

EVIDENCE = ("Small modular reactors produce up to 300 MW of electricity per unit. "
            "Their factory construction lowers upfront capital cost.")


class FakeGateway(ModelGateway):
    def __init__(self, replies: list[Any]) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.prompts.append(request.prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return ModelResponse(text=reply, model_id="meta-llama/llama-3.3-70b-instruct:free")

    async def health_check(self) -> bool:
        return True


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_extract_claims_keeps_cited_sentences_and_skips_bookkeeping() -> None:
    content = ("## Fundamentals\n\nSMRs produce up to 300 MW per unit [1]. This sentence has no citation.\n"
               "- Factory construction lowers capital cost [1, 2].\n\n## Sources\n\n[1] Title - https://x.org")
    claims = extract_claims(content)
    assert [c.text for c in claims] == ["SMRs produce up to 300 MW per unit.", "Factory construction lowers capital cost."]
    assert claims[1].citations == [1, 2]
    assert claims[0].section == "Fundamentals"


def test_heuristic_supported_partial_unsupported_contradicted_and_no_evidence() -> None:
    assert heuristic_verdict("Small modular reactors produce up to 300 MW per unit", [EVIDENCE])[0] == "supported"
    assert heuristic_verdict("Small modular reactors produce 900 MW per unit", [EVIDENCE])[0] == "contradicted"
    assert heuristic_verdict("Factory construction does not lower upfront capital cost", [EVIDENCE])[0] == "contradicted"
    assert heuristic_verdict("Reactors require uranium enrichment and complex waste storage plans", [EVIDENCE])[0] in (
        "unsupported", "partially_supported")
    assert heuristic_verdict("Penguins migrate across Antarctic ice sheets yearly", [EVIDENCE])[0] == "unsupported"
    assert heuristic_verdict("Anything at all", [])[0] == "no_evidence"


def test_verifier_without_gateway_uses_heuristic() -> None:
    claims = extract_claims("Small modular reactors produce up to 300 MW per unit [1].")
    result = run(ClaimVerifier().verify(claims, {1: EVIDENCE}))
    assert result["methods"] == ["heuristic"]
    assert result["claims"][0]["verdict"] == "supported"
    assert result["support_score"] == 1.0


def test_llm_judge_used_and_falls_back_per_claim() -> None:
    gateway = FakeGateway([
        '```json\n{"verdict": "supported", "confidence": 0.9, "rationale": "stated"}\n```',
        ModelError("rate limited", error_code="provider_rate_limit"),
        '{"verdict": "maybe"}',
    ])
    content = ("Small modular reactors produce up to 300 MW per unit [1]. "
               "Factory construction lowers upfront capital cost [1]. "
               "Small modular reactors produce 900 MW per unit [1].")
    result = run(ClaimVerifier(gateway).verify(extract_claims(content), {1: EVIDENCE}))
    rows = result["claims"]
    assert rows[0]["method"].startswith("llm:") and rows[0]["method"].endswith(":free")
    assert rows[1]["method"] == "heuristic" and rows[1]["fallback_reason"] == "rate_limit"
    assert rows[2]["method"] == "heuristic" and rows[2]["fallback_reason"] == "invalid_verdict"
    assert rows[2]["verdict"] == "contradicted"
    assert result["methods"] == ["heuristic", "llm"]
    assert "300 MW" in gateway.prompts[0]


def test_llm_claim_cap_and_budget_hook_failure_fall_back() -> None:
    def hook(_: str) -> None:
        raise RuntimeError("budget exhausted")

    claims = extract_claims("Small modular reactors produce up to 300 MW per unit [1]. Capital cost is lower [1].")
    result = run(ClaimVerifier(FakeGateway([]), budget_hook=hook, max_llm_claims=1).verify(claims, {1: EVIDENCE}))
    assert [r["fallback_reason"] for r in result["claims"]] == ["RuntimeError", "llm_claim_cap"]


def test_markdown_section_lists_verdicts() -> None:
    result = run(ClaimVerifier().verify(extract_claims("SMRs produce up to 300 MW per unit [1]."), {1: EVIDENCE}))
    md = verification_markdown(result)
    assert md.startswith("## Claim verification") and "supported" in md and "| 1 |" in md
    assert "No cited claims" in verification_markdown({"claims": []})


def test_report_agent_adds_verification_before_sources() -> None:
    state = ResearchState(request_id=uuid4())
    state.metadata["live_findings"] = [{
        "kind": "web", "section": "Fundamentals",
        "summary": "Small modular reactors produce up to 300 MW per unit [1].",
        "sources": [{"source_id": "s1", "url": "https://example.org/smr", "title": "SMR", "excerpt": EVIDENCE}],
    }]
    report = run(DeterministicReportAgent().generate_report(state, {"claims": []}))
    content = report["content"]
    assert content.index("## Claim verification") < content.index("## Sources")
    assert report["verification"]["counts"]["supported"] == 1


def test_orchestrator_source_rows_carry_evidence_excerpts() -> None:
    from deep_research.core.orchestration.master_research_orchestrator import _evidence_excerpts
    from deep_research.evidence.evidence import Evidence

    sid = uuid4()
    rows = _evidence_excerpts([Evidence(source_id=sid, content="a" * 3000), object(), Evidence(source_id=uuid4(), content=" ")])
    assert list(rows) == [str(sid)] and len(rows[str(sid)]) == 2000
