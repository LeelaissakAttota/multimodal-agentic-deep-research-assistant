"""Contradiction detection across the claims of a report.

Two kinds of disagreement are reported, both found offline:

* **cross-source** — two cited claims about the same subject (high term
  overlap) that cite different sources and disagree: opposite negation, an
  antonym pair (increase/decrease, safe/unsafe, ...) or conflicting figures
  for the same unit;
* **claim vs source** — a claim the verifier marked ``contradicted``.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from deep_research.verification.fact_check import ExtractedClaim
from deep_research.verification.text import content_terms, jaccard, negated, stem

ANTONYMS: tuple[tuple[str, str], ...] = (
    ("increase", "decrease"), ("rise", "fall"), ("higher", "lower"), ("more", "less"), ("safe", "unsafe"),
    ("cheap", "expensive"), ("benefit", "harm"), ("effective", "ineffective"), ("growth", "decline"),
    ("gain", "loss"), ("improve", "worsen"), ("positive", "negative"), ("support", "oppose"),
    ("strong", "weak"), ("success", "failure"), ("faster", "slower"), ("larger", "smaller"),
)
_FIGURE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(%|percent|mw|gw|kw|kwh|twh|billion|million|thousand|years?|people|usd|\$)",
                     re.IGNORECASE)
_MIN_OVERLAP = 0.3


def _figures(text: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for value, unit in _FIGURE.findall(text):
        unit = unit.lower().rstrip("s") if unit.lower() not in ("%", "percent") else "%"
        out.setdefault(unit, set()).add(value.replace(",", ""))
    return out


def _words(text: str) -> set[str]:
    return {stem(w) for w in re.findall(r"[a-z]+", text.lower())}


def _antonym(a: set[str], b: set[str]) -> tuple[str, str] | None:
    for x, y in ANTONYMS:
        sx, sy = stem(x), stem(y)
        if (sx in a and sy in b and sy not in a and sx not in b) or (sy in a and sx in b and sx not in a and sy not in b):
            return (x, y)
    return None


def find_contradictions(claims: Sequence[ExtractedClaim], verification: Mapping[str, Any] | None = None,
                        *, max_items: int = 20) -> list[dict[str, Any]]:
    """Pairs of disagreeing claims plus claims contradicted by their own sources."""
    found: list[dict[str, Any]] = []
    terms = [content_terms(c.text) for c in claims]
    words = [_words(c.text) for c in claims]
    for i in range(len(claims)):
        for j in range(i + 1, len(claims)):
            a, b = claims[i], claims[j]
            if set(a.citations) == set(b.citations):
                continue  # same source(s): not a cross-source disagreement
            overlap = jaccard(terms[i], terms[j])
            if overlap < _MIN_OVERLAP:
                continue
            reason = _disagreement(a.text, b.text, words[i], words[j])
            if reason:
                found.append({"kind": "cross_source", "claims": [a.text, b.text],
                              "citations": [a.citations, b.citations], "reason": reason,
                              "overlap": round(overlap, 2)})
    for row in (verification or {}).get("claims") or []:
        if isinstance(row, dict) and row.get("verdict") == "contradicted":
            found.append({"kind": "claim_vs_source", "claims": [row.get("claim")], "citations": [row.get("citations")],
                          "reason": str(row.get("rationale") or "the cited source disagrees")})
    return found[:max_items]


def _disagreement(a: str, b: str, wa: set[str], wb: set[str]) -> str | None:
    if negated(a) != negated(b):
        return "one claim negates the other"
    pair = _antonym(wa, wb)
    if pair:
        return f"opposite direction ({pair[0]} vs {pair[1]})"
    fa, fb = _figures(a), _figures(b)
    for unit in fa.keys() & fb.keys():
        if fa[unit].isdisjoint(fb[unit]):
            return f"conflicting figures ({', '.join(sorted(fa[unit]))} vs {', '.join(sorted(fb[unit]))} {unit})"
    return None


def contradictions_markdown(items: Sequence[Mapping[str, Any]], *, claim_count: int) -> str:
    title = "## Contradictions and disagreements"
    if not items:
        return f"{title}\n\nNo contradictions detected among {claim_count} cited claims."
    lines = []
    for item in items:
        cites = " vs ".join("[" + ", ".join(str(n) for n in c or []) + "]" for c in item.get("citations") or [])
        quoted = " ⟷ ".join(f"“{c}”" for c in item.get("claims") or [])
        label = "Sources disagree" if item.get("kind") == "cross_source" else "Claim not backed by its source"
        lines.append(f"- **{label}** {cites}: {quoted} — {item.get('reason')}")
    return f"{title}\n\n{len(items)} potential conflict(s) found; check the cited sources before relying on them.\n\n" + \
        "\n".join(lines)
