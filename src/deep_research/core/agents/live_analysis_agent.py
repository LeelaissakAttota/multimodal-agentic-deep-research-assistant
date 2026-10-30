"""Live-mode analysis: turn what was actually read into source-linked claims.

``LiveAnalysisAgent`` replaces the placeholder claims ("Claim based on evidence ...")
for live runs. It reads the excerpts kept in ``state.metadata["live_findings"]`` and

* with a model gateway, asks the configured (free) model for short factual claims, each
  citing the numbered excerpts it rests on. The reply is validated strictly: unknown
  source numbers are dropped and claims without a valid citation are discarded;
* without a model, or on any model failure (rate limit, timeout, invalid JSON, no valid
  claims), builds claims extractively from the first informative sentence of each excerpt.

When a run has no live findings (offline mode) it defers to ``DeterministicAnalysisAgent``.
Every decision is recorded in the run's decision log with its ``source``.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List
from uuid import UUID, uuid5

from deep_research.context.context_builder import AgentContext
from deep_research.core.agents.analysis_agent import AnalysisAgent, DeterministicAnalysisAgent
from deep_research.core.agents.llm_support import AgentDecisionError, ask_json, clean_text, record_decision
from deep_research.domain.research.research_plan import ResearchPlan
from deep_research.domain.research.research_state import ResearchState
from deep_research.evidence.claim import Claim

_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_NS = UUID("6f1c2a5e-3b7d-4c41-9a8e-2d5b7c9e1f00")
MAX_SOURCES = 12
EXCERPT_CHARS = 700
CLAIM_CHARS = 300

PROMPT = """You are the analysis (claim extraction) agent of a research assistant.
Research question: {objective}

Numbered source excerpts:
{excerpts}

Extract up to {max_claims} short, factual claims that directly help answer the question.
Use ONLY the excerpts. Each claim must cite the excerpt numbers that support it.
Reply with JSON only:
{{"claims": [{{"text": "...", "sources": [1], "confidence": 0.0-1.0}}], "summary": "2-3 sentences", "gaps": ["..."]}}"""


def _sources(state: ResearchState) -> List[Dict[str, Any]]:
    """Unique sources with a non-empty excerpt, in the order they were read."""
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    findings = state.metadata.get("live_findings")
    for finding in findings if isinstance(findings, list) else []:
        if not isinstance(finding, dict):
            continue
        for row in finding.get("sources") or []:
            if not isinstance(row, dict):
                continue
            sid, excerpt = str(row.get("source_id") or ""), row.get("excerpt")
            if not sid or sid in seen or not isinstance(excerpt, str) or not excerpt.strip():
                continue
            seen.add(sid)
            out.append({"source_id": sid, "title": str(row.get("title") or ""), "excerpt": excerpt.strip()})
    return out[:MAX_SOURCES]


def _support_ids(source: Dict[str, Any]) -> List[UUID]:
    try:
        return [UUID(source["source_id"])]
    except (ValueError, KeyError, TypeError):
        return []


def first_sentence(text: str) -> str:
    """First informative sentence of an excerpt (skips very short fragments)."""
    for sentence in _SENTENCE.split(" ".join(text.split())):
        sentence = sentence.strip()
        if len(sentence.split()) >= 6:
            return sentence[:CLAIM_CHARS]
    return ""


class LiveAnalysisAgent(AnalysisAgent):
    """Source-grounded claims for live runs, LLM first with an extractive fallback."""

    def __init__(
        self,
        gateway: Any | None = None,
        *,
        timeout_seconds: float = 30.0,
        max_claims: int = 8,
        budget_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.gateway = gateway
        self.timeout_seconds = timeout_seconds
        self.max_claims = max_claims
        self.budget_hook = budget_hook
        self.fallback = DeterministicAnalysisAgent()

    async def analyze(
        self,
        state: ResearchState,
        plan: ResearchPlan,
        context: AgentContext | None = None,
    ) -> Dict[str, Any]:
        sources = _sources(state)
        if not sources:
            return await self.fallback.analyze(state, plan, context)
        if self.gateway is not None:
            try:
                result, model_id = await self._llm(state, plan, sources)
            except AgentDecisionError as exc:
                result = self._extractive(state, sources)
                record_decision(state, {"agent": "analysis", "source": "deterministic",
                                        "fallback_reason": exc.reason, "detail": exc.detail[:200],
                                        "claims": len(result["claims"])})
                return result
            record_decision(state, {"agent": "analysis", "source": "llm", "model_id": model_id,
                                    "claims": len(result["claims"])})
            return result
        result = self._extractive(state, sources)
        record_decision(state, {"agent": "analysis", "source": "deterministic", "fallback_reason": "no_model",
                                "claims": len(result["claims"])})
        return result

    async def _llm(self, state: ResearchState, plan: ResearchPlan,
                   sources: List[Dict[str, Any]]) -> tuple[Dict[str, Any], str]:
        excerpts = "\n\n".join(
            f"[{i}] {s['title']}\n{s['excerpt'][:EXCERPT_CHARS]}" for i, s in enumerate(sources, start=1)
        )
        prompt = PROMPT.format(objective=plan.objective, excerpts=excerpts, max_claims=self.max_claims)
        if self.budget_hook is not None:
            self.budget_hook("analysis")
        reply, model_id = await ask_json(self.gateway, prompt, max_tokens=900,
                                         timeout_seconds=self.timeout_seconds)
        return self._validate(reply, state, sources), model_id

    def _validate(self, reply: Dict[str, Any], state: ResearchState,
                  sources: List[Dict[str, Any]]) -> Dict[str, Any]:
        raw = reply.get("claims")
        if not isinstance(raw, list):
            raise AgentDecisionError("schema", "claims is not a list")
        claims: List[Claim] = []
        for item in raw[: self.max_claims]:
            if not isinstance(item, dict):
                continue
            text = clean_text(item.get("text"), max_len=CLAIM_CHARS)
            nums = item.get("sources")
            numbers = [n for n in nums if isinstance(n, int) and 1 <= n <= len(sources)] if isinstance(nums, list) else []
            support = [sid for n in dict.fromkeys(numbers) for sid in _support_ids(sources[n - 1])]
            if not text or not support:
                continue
            conf = item.get("confidence")
            confidence = float(conf) if isinstance(conf, (int, float)) and 0 <= conf <= 1 else 0.7
            claims.append(Claim(id=uuid5(_NS, f"{state.id}:{text}"), text=text, supported_by=support,
                                confidence=confidence, metadata={"analysis": "llm", "sources": numbers}))
        if not claims:
            raise AgentDecisionError("no_valid_claims", "no claim cited a known excerpt")
        summary = clean_text(reply.get("summary"), max_len=800) or self._summary(claims, sources)
        gaps_raw = reply.get("gaps")
        gaps = [g for g in (clean_text(x, max_len=200) for x in gaps_raw) if g][:5] if isinstance(gaps_raw, list) else []
        return {"claims": claims, "evidence_gathered": state.gathered_evidence, "summary": summary, "gaps": gaps,
                "analysis_mode": "llm"}

    def _extractive(self, state: ResearchState, sources: List[Dict[str, Any]]) -> Dict[str, Any]:
        claims: List[Claim] = []
        seen: set[str] = set()
        for i, source in enumerate(sources, start=1):
            sentence = first_sentence(source["excerpt"])
            support = _support_ids(source)
            if not sentence or not support or sentence.lower() in seen:
                continue
            seen.add(sentence.lower())
            claims.append(Claim(id=uuid5(_NS, f"{state.id}:{sentence}"), text=sentence, supported_by=support,
                                confidence=0.5, metadata={"analysis": "extractive", "sources": [i]}))
            if len(claims) >= self.max_claims:
                break
        gaps = ["More evidence needed for comprehensive analysis"] if len(sources) < 2 else []
        return {"claims": claims, "evidence_gathered": state.gathered_evidence,
                "summary": self._summary(claims, sources), "gaps": gaps, "analysis_mode": "extractive"}

    @staticmethod
    def _summary(claims: List[Claim], sources: List[Dict[str, Any]]) -> str:
        return f"Analysis completed. Found {len(claims)} source-linked claims from {len(sources)} sources read."
