"""Offline report evaluation (no network, no model calls, deterministic).

Metrics, each in ``[0, 1]``:

* **faithfulness** — share of cited claims backed by the excerpts of the
  sources they cite (offline heuristic verifier; partial = half credit,
  contradicted claims count as zero). With no checkable claims it is ``0``.
* **relevance** — how on-topic the claims are: mean over claims of the share
  of objective terms… blended 50/50 with the share of claims touching the
  objective at all.
* **coverage** — share of the case's expected key points that some claim
  covers (≥ 50 % of the point's terms); without expected points, the share of
  report sections that contain at least one cited claim.

``overall`` is the unweighted mean. ``run_suite`` scores the bundled cases
(``cases.CASES``) or any list of :class:`EvalCase`.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

from deep_research.verification.fact_check import ClaimVerifier, ExtractedClaim, extract_claims
from deep_research.verification.text import content_terms


@dataclass
class EvalCase:
    name: str
    objective: str
    report: str
    excerpts: dict[int, str]
    expected_points: list[str] = field(default_factory=list)
    expect: str = ""  # free-text note on what the case exercises


@dataclass
class Scorecard:
    name: str
    faithfulness: float
    relevance: float
    coverage: float
    claims: int
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def overall(self) -> float:
        return round((self.faithfulness + self.relevance + self.coverage) / 3, 3)

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "overall": self.overall}


async def score_report_async(objective: str, report: str, excerpts: Mapping[int, str], *,
                             expected_points: Sequence[str] = (), name: str = "report") -> Scorecard:
    """Async scorer for callers already inside an event loop (API handlers, notebooks, agents)."""
    claims = extract_claims(report, max_claims=200)
    verification: Mapping[str, Any] = (await ClaimVerifier().verify(claims, excerpts)) if claims \
        else {"counts": {}, "claims": []}
    faithfulness = _faithfulness(verification)
    relevance, rel_detail = _relevance(objective, claims)
    coverage, cov_detail = _coverage(report, claims, expected_points)
    return Scorecard(name=name, faithfulness=faithfulness, relevance=relevance, coverage=coverage, claims=len(claims),
                     details={"verdicts": verification.get("counts", {}), **rel_detail, **cov_detail})


def score_report(objective: str, report: str, excerpts: Mapping[int, str], *, expected_points: Sequence[str] = (),
                 name: str = "report") -> Scorecard:
    """Synchronous scorer; safe to call with or without a running event loop.

    Inside a running loop the coroutine is executed on a short-lived worker thread
    with its own loop (``asyncio.run`` would raise there).
    """
    coro_args = (objective, report, excerpts)
    kwargs: dict[str, Any] = {"expected_points": expected_points, "name": name}
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(score_report_async(*coro_args, **kwargs))
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(score_report_async(*coro_args, **kwargs))).result()


def _faithfulness(verification: Mapping[str, Any]) -> float:
    rows = [r for r in verification.get("claims") or [] if r.get("verdict") != "no_evidence"]
    if not rows:
        return 0.0
    credit = {"supported": 1.0, "partially_supported": 0.5}
    return round(sum(credit.get(str(r.get("verdict")), 0.0) for r in rows) / len(rows), 3)


def _relevance(objective: str, claims: Sequence[ExtractedClaim]) -> tuple[float, dict[str, Any]]:
    goal = content_terms(objective)
    if not goal or not claims:
        return 0.0, {"on_topic_claims": 0}
    shares = [len(goal & content_terms(c.text)) / len(goal) for c in claims]
    on_topic = sum(1 for s in shares if s > 0)
    score = 0.5 * (sum(shares) / len(shares)) + 0.5 * (on_topic / len(claims))
    return round(min(1.0, score), 3), {"on_topic_claims": on_topic}


def _coverage(report: str, claims: Sequence[ExtractedClaim], points: Sequence[str]) -> tuple[float, dict[str, Any]]:
    if points:
        claim_terms = [content_terms(c.text) for c in claims]
        missed = []
        for point in points:
            want = content_terms(point)
            if not want or not any(len(want & t) / len(want) >= 0.5 for t in claim_terms):
                missed.append(point)
        return round(1 - len(missed) / len(points), 3), {"missed_points": missed}
    sections = {line.lstrip("#").strip() for line in report.splitlines() if line.startswith("## ")}
    sections -= {"Sources", "Attached files", "Claim verification", "Contradictions and disagreements", "Charts"}
    if not sections:
        return (1.0 if claims else 0.0), {"sections": 0}
    covered = {c.section for c in claims} & sections
    return round(len(covered) / len(sections), 3), {"sections": len(sections), "sections_with_claims": len(covered)}


def run_suite(cases: Sequence[EvalCase] | None = None) -> list[Scorecard]:
    if cases is None:
        from deep_research.evaluation.cases import CASES

        cases = CASES
    return [score_report(c.objective, c.report, c.excerpts, expected_points=c.expected_points, name=c.name)
            for c in cases]


def scorecard_table(cards: Sequence[Scorecard]) -> str:
    header = f"{'case':<28} {'faithful':>8} {'relevant':>8} {'coverage':>8} {'overall':>8} {'claims':>6}"
    rows = [f"{c.name[:28]:<28} {c.faithfulness:>8.2f} {c.relevance:>8.2f} {c.coverage:>8.2f} {c.overall:>8.2f} "
            f"{c.claims:>6}" for c in cards]
    if cards:
        n = len(cards)
        rows.append("-" * len(header))
        rows.append(f"{'mean':<28} {sum(c.faithfulness for c in cards) / n:>8.2f} "
                    f"{sum(c.relevance for c in cards) / n:>8.2f} {sum(c.coverage for c in cards) / n:>8.2f} "
                    f"{sum(c.overall for c in cards) / n:>8.2f} {sum(c.claims for c in cards):>6}")
    return "\n".join([header, "-" * len(header), *rows])


def case_from_run(run: Mapping[str, Any], *, name: str | None = None) -> EvalCase | None:
    """Build a case from a saved dashboard/control run JSON (needs a report with citation excerpts)."""
    result = run.get("result") if isinstance(run.get("result"), Mapping) else run
    report = result.get("report") if isinstance(result, Mapping) else None
    if not isinstance(report, Mapping):
        return None
    raw_params = run.get("params")
    params: Mapping[str, Any] = raw_params if isinstance(raw_params, Mapping) else {}
    objective = str(params.get("objective") or report.get("title") or "")
    excerpts = {int(c["number"]): str(c.get("excerpt") or "") for c in report.get("citations") or []
                if isinstance(c, Mapping) and isinstance(c.get("number"), int)}
    return EvalCase(name=name or str(run.get("run_id") or "run"), objective=objective,
                    report=str(report.get("content") or ""), excerpts=excerpts)
