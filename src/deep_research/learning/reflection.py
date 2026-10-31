"""Post-run reflection: score a finished run from existing signals and write 1-3 lessons.

Signals: citation count/coverage, source credibility, failed/timed-out tools, run time, the
evaluator's quality scores and user feedback (when present). An LLM (the configured provider)
writes the lessons; on any failure the deterministic rules below write them instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from deep_research.core.agents.llm_support import AgentDecisionError, ask_json, clean_text

TOPICS: dict[str, tuple[str, ...]] = {
    "finance": ("stock", "stocks", "market", "interest", "inflation", "earnings", "invest", "bank", "crypto", "bitcoin",
                "price", "economy", "gdp", "revenue", "fund", "tax"),
    "health": ("health", "disease", "drug", "vaccine", "diet", "fasting", "cancer", "medical", "clinical", "covid"),
    "technology": ("ai", "software", "model", "llm", "chip", "computer", "cloud", "python", "data", "robot"),
    "science": ("climate", "energy", "nuclear", "reactor", "physics", "biology", "species", "carbon", "space"),
    "policy": ("law", "policy", "regulation", "government", "election", "court", "rights"),
}
AUTHORITY_HINTS = {
    "finance": "SEC filings, central-bank and official statistics",
    "health": "WHO, NIH, peer-reviewed clinical studies",
    "technology": "official docs, standards bodies and academic papers",
    "science": "peer-reviewed papers and government science agencies",
    "policy": "official government and court sources",
    "general": "official, academic or encyclopedic sources",
}
_WORD = re.compile(r"[a-z0-9]+")

REFLECT_PROMPT = """{preamble}You are reflecting on a research run you just finished, to improve future runs.
Run signals (JSON): {signals}
Write 1 to 3 short, general, reusable lessons (max 25 words each) that would make a FUTURE run on a similar
question better. Be specific about sources or tools. No personal data, no secrets, no question-specific facts.
Return ONLY a JSON object: {{"lessons": [{{"text": "...", "confidence": 0.0-1.0}}]}}"""


def classify_topic(text: str) -> str:
    words = set(_WORD.findall(text.lower()))
    best = max(TOPICS, key=lambda t: len(words & set(TOPICS[t])))
    return best if words & set(TOPICS[best]) else "general"


@dataclass
class RunSignals:
    run_id: str
    topic: str
    status: str
    elapsed_seconds: float
    citations: int
    claims: int
    supported_claims: int
    credible_citations: int
    tool_calls: int
    failed_tools: list[str] = field(default_factory=list)
    slow_tools: list[str] = field(default_factory=list)
    quality: float | None = None  # evaluator quality average, 0-5
    feedback: float | None = None  # user rating, 0-1
    lessons_applied: int = 0
    failure_reason: str | None = None  # error code / kind only (never user text), for failed runs

    @property
    def coverage(self) -> float:
        return self.supported_claims / self.claims if self.claims else 0.0

    def score(self) -> float:
        """0-100 run score: evidence (45) + credibility (15) + reliability (15) + speed (10) + quality/feedback (15)."""
        if self.status == "failed":
            return 0.0
        evidence = min(self.citations / 6, 1.0) * 25 + self.coverage * 20
        credibility = (self.credible_citations / self.citations) * 15 if self.citations else 0.0
        reliability = 15 * (1 - len(self.failed_tools) / self.tool_calls) if self.tool_calls else 15.0
        speed = 10.0 if self.elapsed_seconds <= 120 else max(0.0, 10 - (self.elapsed_seconds - 120) / 30)
        extras = [x for x in ((self.quality / 5) if self.quality is not None else None, self.feedback) if x is not None]
        judged = (sum(extras) / len(extras) * 15) if extras else 7.5
        return round(evidence + credibility + reliability + speed + judged, 1)

    def as_dict(self) -> dict[str, Any]:
        return {"topic": self.topic, "status": self.status, "elapsed_seconds": round(self.elapsed_seconds, 1),
                "citations": self.citations, "claims": self.claims, "claim_coverage": round(self.coverage, 2),
                "credible_citations": self.credible_citations, "tool_calls": self.tool_calls,
                "failed_tools": self.failed_tools, "slow_tools": self.slow_tools, "quality": self.quality,
                "feedback": self.feedback, "lessons_applied": self.lessons_applied, "score": self.score(),
                **({"failure_reason": self.failure_reason} if self.failure_reason else {})}


def collect_signals(run_id: str, objective: str, status: str, report: Mapping[str, Any] | None,
                    runtime: Mapping[str, Any] | None, tool_events: Sequence[Mapping[str, Any]],
                    metadata: Mapping[str, Any] | None = None, *, elapsed_seconds: float = 0.0,
                    slow_ms: float = 30_000) -> RunSignals:
    report = report or {}
    citations = [c for c in report.get("citations") or [] if isinstance(c, Mapping)]
    claims = [c for c in report.get("claims") or [] if isinstance(c, Mapping)]
    credible = sum(1 for c in citations if _cred_score(c) >= 0.7)
    supported = sum(1 for c in claims if c.get("supported_by") or c.get("sources") or c.get("evidence_ids"))
    quality = None
    for decision in reversed(list((metadata or {}).get("agent_decisions") or [])):
        scores = decision.get("scores") if isinstance(decision, Mapping) else None
        if isinstance(scores, Mapping):
            vals = [float(v) for v in scores.values() if isinstance(v, (int, float))]
            quality = round(sum(vals) / len(vals), 2) if vals else None
            break
    feedback = (metadata or {}).get("user_feedback")
    failure = (metadata or {}).get("runtime_failure")
    failure_reason = None
    if str(status) == "failed":
        if isinstance(failure, Mapping):
            failure_reason = ":".join(str(failure.get(k)) for k in ("failure_kind", "error_code") if failure.get(k))[:80]
        failure_reason = failure_reason or "failed"
    usage = (runtime or {}).get("usage") or {}
    return RunSignals(
        run_id=run_id, topic=classify_topic(objective), status=str(status), citations=len(citations),
        elapsed_seconds=float(elapsed_seconds or usage.get("elapsed_seconds") or 0.0),
        claims=len(claims), supported_claims=supported, credible_citations=credible,
        tool_calls=len(tool_events) or int(usage.get("tool_calls") or 0),
        failed_tools=sorted({str(e["tool"]) for e in tool_events if not e.get("success")}),
        slow_tools=sorted({str(e["tool"]) for e in tool_events if float(e.get("latency_ms") or 0) >= slow_ms}),
        quality=quality, feedback=float(feedback) if isinstance(feedback, (int, float)) else None,
        failure_reason=failure_reason,
    )


def _cred_score(citation: Mapping[str, Any]) -> float:
    cred = citation.get("credibility")
    if isinstance(cred, Mapping) and isinstance(cred.get("score"), (int, float)):
        return float(cred["score"])
    from deep_research.evidence.scoring import credibility

    return float(credibility(citation.get("url"), kind=citation.get("kind"))["score"])


def deterministic_lessons(s: RunSignals) -> list[tuple[str, float]]:
    """Rule-based lessons, most important first (max 3)."""
    out: list[tuple[str, float]] = []
    for tool in s.failed_tools[:1]:
        out.append((f"Tool {tool} failed or timed out; lower its priority and keep a fallback search ready.", 0.6))
    for tool in [t for t in s.slow_tools if t not in s.failed_tools][:1]:
        out.append((f"Tool {tool} was slow (over 30 s); use it only when the question needs it.", 0.5))
    if s.citations < 3:
        out.append((f"For {s.topic} questions, plan more distinct queries so the report has at least 3 citations.", 0.55))
    if s.citations and s.credible_citations / s.citations < 0.5:
        out.append((f"For {s.topic} questions, prefer {AUTHORITY_HINTS[s.topic]} over blogs or SEO pages.", 0.6))
    if s.claims and s.coverage < 0.7:
        out.append(("Keep only claims that cite a gathered source; drop unsupported claims before writing.", 0.5))
    if s.elapsed_seconds > 180:
        out.append(("Runs over 3 minutes risk the time limit; use fewer, more precise queries.", 0.5))
    if not out and s.score() >= 70:
        out.append((f"For {s.topic} questions, the current plan works well; keep {AUTHORITY_HINTS[s.topic]} first.", 0.45))
    return out[:3]


async def reflect(signals: RunSignals, *, gateway: Any = None, preamble: str = "",
                  timeout_seconds: float = 30.0) -> tuple[list[tuple[str, float]], str]:
    """Return ``(lessons, source)``; source is ``llm`` or ``deterministic``."""
    if gateway is not None:
        import json

        try:
            reply, _ = await ask_json(gateway, REFLECT_PROMPT.format(preamble=preamble,
                                                                     signals=json.dumps(signals.as_dict())),
                                      max_tokens=400, timeout_seconds=timeout_seconds)
            raw = reply.get("lessons")
            if not isinstance(raw, list):
                raise AgentDecisionError("schema", "lessons is not a list")
            lessons = []
            for item in raw[:3]:
                text = clean_text(item.get("text") if isinstance(item, dict) else item, max_len=220)
                conf = item.get("confidence") if isinstance(item, dict) else None
                if text:
                    lessons.append((text, float(conf) if isinstance(conf, (int, float)) and 0 <= conf <= 1 else 0.6))
            if lessons:
                return lessons, "llm"
        except (AgentDecisionError, AttributeError, TypeError, ValueError):
            pass
    return deterministic_lessons(signals), "deterministic"
