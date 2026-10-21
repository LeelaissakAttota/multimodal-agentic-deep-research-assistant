"""Deterministic step risk classification for supervised control.

LOW: data fetching, formatting, non-destructive reads, analysis, report rendering.
HIGH: sending messages, modifying/deleting data, spending money, publishing,
key changes -- and anything not explicitly listed (unknown defaults to HIGH).
"""

from __future__ import annotations

from enum import StrEnum


class RiskLevel(StrEnum):
    LOW = "low"
    HIGH = "high"


#: Step kinds that are known to be read-only / non-destructive.
LOW_RISK_KINDS: frozenset[str] = frozenset(
    {
        "fetch",
        "read",
        "search",
        "format",
        "plan",
        "analyze",
        "evaluate",
        "render_report",
        "research_task",
        "planning",
        "analysis",
        "evaluation",
        "reporting",
        "company_resolution",
        "market_intelligence",
        "financial_intelligence",
        "news_event_intelligence",
        "industry_intelligence",
        "regulatory_intelligence",
    }
)

#: Explicitly high-risk kinds (listed for documentation; unknown is HIGH anyway).
HIGH_RISK_KINDS: frozenset[str] = frozenset(
    {
        "send_message",
        "notify",
        "write",
        "modify",
        "delete",
        "spend",
        "purchase",
        "publish",
        "key_change",
    }
)


def classify_step(kind: str | None) -> RiskLevel:
    """Classify a step kind. Unknown, empty or explicitly risky kinds are HIGH."""

    if not kind:
        return RiskLevel.HIGH
    normalized = kind.strip().lower()
    if normalized in HIGH_RISK_KINDS:
        return RiskLevel.HIGH
    if normalized in LOW_RISK_KINDS:
        return RiskLevel.LOW
    return RiskLevel.HIGH
