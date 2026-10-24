"""Evidence verification: per-claim fact checking, contradiction detection and report charts."""

from deep_research.verification.fact_check import (
    ClaimVerifier,
    VERDICTS,
    extract_claims,
    heuristic_verdict,
    verification_markdown,
)

__all__ = ["ClaimVerifier", "VERDICTS", "extract_claims", "heuristic_verdict", "verification_markdown"]
