"""Per-claim fact verification.

Each cited sentence of a report is treated as a claim and checked against the
excerpts of the sources it cites. A free LLM judge (any guarded
``ModelGateway``) decides when one is configured; every claim it cannot judge
(no gateway, rate limit, invalid JSON, timeout, over the call budget) falls
back to an offline lexical heuristic, so verification never needs the network.
Each verdict records which ``method`` produced it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from deep_research.verification.text import (
    citation_numbers,
    content_terms,
    negated,
    numbers,
    strip_citations,
)

VERDICTS = ("supported", "partially_supported", "unsupported", "contradicted", "no_evidence")
_SKIP_SECTIONS = {"sources", "attached files", "agent decisions", "claim verification",
                  "contradictions and disagreements", "charts"}
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")
_MAX_EVIDENCE_CHARS = 1800

JUDGE_PROMPT = (
    "You are a strict fact-checking judge. Decide whether the EVIDENCE supports the CLAIM.\n"
    "Answer with ONE JSON object only: {{\"verdict\": one of [\"supported\", \"partially_supported\", "
    "\"unsupported\", \"contradicted\"], \"confidence\": number 0-1, \"rationale\": short string}}.\n"
    "Use \"contradicted\" only when the evidence states the opposite or a conflicting figure.\n\n"
    "CLAIM: {claim}\n\nEVIDENCE:\n{evidence}\n\nJSON:"
)


@dataclass
class ClaimCheck:
    """Verdict for one claim."""

    claim: str
    citations: list[int]
    section: str
    verdict: str
    confidence: float
    method: str
    rationale: str = ""
    fallback_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {"claim": self.claim, "citations": list(self.citations), "section": self.section,
                               "verdict": self.verdict, "confidence": round(self.confidence, 2),
                               "method": self.method, "rationale": self.rationale}
        if self.fallback_reason:
            row["fallback_reason"] = self.fallback_reason
        return row


@dataclass
class ExtractedClaim:
    text: str
    citations: list[int]
    section: str = "Findings"
    extra: dict[str, Any] = field(default_factory=dict)


def extract_claims(content: str, *, max_claims: int = 60) -> list[ExtractedClaim]:
    """Cited sentences of a Markdown report body, with their section heading.

    Bookkeeping sections (Sources, Attached files, agent decisions and the
    verification sections themselves) are skipped; uncited sentences are not
    treated as claims because there is nothing to check them against.
    """
    claims: list[ExtractedClaim] = []
    section = "Findings"
    skipping = False
    for raw in content.splitlines():
        line = raw.strip()
        heading = re.match(r"^#{1,6}\s+(.*)$", line)
        if heading:
            section = heading.group(1).strip()
            skipping = section.lower() in _SKIP_SECTIONS
            continue
        if skipping or not line or line.startswith("|"):
            continue
        line = re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", line)
        for sentence in _SENTENCE.split(line):
            cited = citation_numbers(sentence)
            text = " ".join(strip_citations(sentence).replace("**", "").split()).strip(" -")
            text = re.sub(r"\s+([.,;:!?])", r"\1", text)
            if cited and len(text) >= 12:
                claims.append(ExtractedClaim(text=text, citations=cited, section=section))
                if len(claims) >= max_claims:
                    return claims
    return claims


def heuristic_verdict(claim: str, evidence: Iterable[str]) -> tuple[str, float, str]:
    """Offline verdict from term overlap, numeric agreement and negation polarity.

    Returns ``(verdict, confidence, rationale)``. Deterministic and network-free.
    """
    texts = [t for t in evidence if t and t.strip()]
    if not texts:
        return "no_evidence", 0.0, "no source text available for the cited sources"
    terms = content_terms(claim)
    if not terms:
        return "unsupported", 0.2, "claim has no checkable content words"
    best_text, best_cov = "", -1.0
    for text in texts:
        cov = len(terms & content_terms(text)) / len(terms)
        if cov > best_cov:
            best_text, best_cov = text, cov
    claim_nums = numbers(claim)
    ev_nums = set().union(*(numbers(t) for t in texts))
    missing = sorted(n for n in claim_nums if n not in ev_nums and n.rstrip("%") not in ev_nums)
    polarity_flip = best_cov >= 0.5 and negated(claim) != negated(_best_window(best_text, terms))
    if polarity_flip:
        return "contradicted", 0.55, f"cited text matches {best_cov:.0%} of terms but with opposite negation"
    if claim_nums and missing and len(missing) == len(claim_nums) and best_cov >= 0.5 and ev_nums:
        return "contradicted", 0.5, f"figures {', '.join(missing)} differ from the source's figures"
    if best_cov >= 0.6 and not missing:
        return "supported", min(0.95, 0.5 + best_cov / 2), f"{best_cov:.0%} of claim terms found in the cited text"
    if best_cov >= 0.3:
        why = f"{best_cov:.0%} of claim terms found"
        if missing:
            why += f"; figures not found: {', '.join(missing)}"
        return "partially_supported", 0.4 + best_cov / 4, why
    return "unsupported", 0.6 - best_cov, f"only {best_cov:.0%} of claim terms found in the cited text"


def _best_window(text: str, terms: set[str]) -> str:
    """The evidence sentence sharing most terms with the claim (polarity is judged locally)."""
    sentences = _SENTENCE.split(text) or [text]
    return max(sentences, key=lambda s: len(terms & content_terms(s)))


class ClaimVerifier:
    """Verify claims with a free LLM judge, falling back to the offline heuristic per claim."""

    def __init__(self, gateway: Any | None = None, *, max_llm_claims: int = 12, timeout_seconds: float = 30.0,
                 budget_hook: Any | None = None) -> None:
        self.gateway = gateway
        self.max_llm_claims = max_llm_claims
        self.timeout_seconds = timeout_seconds
        self.budget_hook = budget_hook

    async def verify(self, claims: list[ExtractedClaim], excerpts: Mapping[int, str]) -> dict[str, Any]:
        checks: list[ClaimCheck] = []
        llm_calls = 0
        for claim in claims:
            evidence = [excerpts[n] for n in claim.citations if excerpts.get(n)]
            check: ClaimCheck | None = None
            reason: str | None = None
            if self.gateway is None:
                reason = "no_gateway"
            elif not evidence:
                reason = "no_evidence"
            elif llm_calls >= self.max_llm_claims:
                reason = "llm_claim_cap"
            else:
                llm_calls += 1
                check, reason = await self._judge(claim, evidence)
            if check is None:
                verdict, conf, why = heuristic_verdict(claim.text, evidence)
                check = ClaimCheck(claim.text, claim.citations, claim.section, verdict, conf, "heuristic", why,
                                   fallback_reason=reason if reason not in (None, "no_gateway") else None)
            checks.append(check)
        return summarize(checks)

    async def _judge(self, claim: ExtractedClaim, evidence: list[str]) -> tuple[ClaimCheck | None, str | None]:
        from deep_research.core.agents.llm_support import AgentDecisionError, ask_json

        assert self.gateway is not None
        joined = "\n---\n".join(e[:_MAX_EVIDENCE_CHARS] for e in evidence)[: _MAX_EVIDENCE_CHARS * 2]
        try:
            if self.budget_hook is not None:
                self.budget_hook("claim_verification")
            reply, model_id = await ask_json(self.gateway, JUDGE_PROMPT.format(claim=claim.text, evidence=joined),
                                             max_tokens=300, timeout_seconds=self.timeout_seconds)
        except AgentDecisionError as exc:
            return None, exc.reason
        except Exception as exc:  # budget exhaustion or provider bug: fall back, never fail the report
            return None, type(exc).__name__
        verdict = str(reply.get("verdict", "")).strip().lower().replace(" ", "_")
        if verdict not in VERDICTS[:4]:
            return None, "invalid_verdict"
        try:
            confidence = max(0.0, min(1.0, float(reply.get("confidence", 0.5))))
        except (TypeError, ValueError):
            confidence = 0.5
        rationale = " ".join(str(reply.get("rationale") or "").split())[:240]
        return ClaimCheck(claim.text, claim.citations, claim.section, verdict, confidence, f"llm:{model_id}",
                          rationale), None


def summarize(checks: list[ClaimCheck]) -> dict[str, Any]:
    counts = {v: 0 for v in VERDICTS}
    for check in checks:
        counts[check.verdict] = counts.get(check.verdict, 0) + 1
    checked = len(checks) - counts["no_evidence"]
    score = ((counts["supported"] + 0.5 * counts["partially_supported"]) / checked) if checked else 0.0
    methods = sorted({"llm" if c.method.startswith("llm") else "heuristic" for c in checks})
    return {"claims": [c.as_dict() for c in checks], "counts": counts, "total": len(checks),
            "support_score": round(score, 3), "methods": methods}


_LABEL = {"supported": "✅ supported", "partially_supported": "🟡 partial", "unsupported": "⚪ unsupported",
          "contradicted": "❌ contradicted", "no_evidence": "– no evidence"}


def verification_markdown(result: Mapping[str, Any], *, max_rows: int = 25) -> str:
    rows = [r for r in result.get("claims") or [] if isinstance(r, dict)]
    if not rows:
        return "## Claim verification\n\nNo cited claims to verify."
    counts = result.get("counts") or {}
    head = (f"## Claim verification\n\n{len(rows)} cited claims checked "
            f"({', '.join(result.get('methods') or [])}); support score "
            f"{float(result.get('support_score') or 0):.0%}. "
            + ", ".join(f"{_LABEL[v]}: {counts.get(v, 0)}" for v in VERDICTS if counts.get(v)) + ".\n\n"
            "| # | Claim | Cites | Verdict |\n|---|---|---|---|")
    lines = []
    for i, row in enumerate(rows[:max_rows], start=1):
        text = str(row.get("claim", "")).replace("|", "\\|")
        text = text if len(text) <= 140 else text[:139] + "…"
        cites = ", ".join(str(n) for n in row.get("citations") or [])
        lines.append(f"| {i} | {text} | [{cites}] | {_LABEL.get(str(row.get('verdict')), row.get('verdict'))} |")
    more = f"\n\n_{len(rows) - max_rows} more claims in the report data._" if len(rows) > max_rows else ""
    return head + "\n" + "\n".join(lines) + more
