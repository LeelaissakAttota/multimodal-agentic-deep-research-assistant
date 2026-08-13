"""Offline Phase 7 demo scenarios for the bounded product integration."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence

from deep_research.api.research_service import (
    ResearchApplication,
    ResearchRunResponse,
    ResearchSubmission,
)
from deep_research.core.config import Settings

DEFAULT_DEMO_OBJECTIVES = (
    "Assess reliability considerations for grid-scale battery storage",
    "Compare evidence needs for responsible multimodal research systems",
)
MAX_DEMO_SCENARIOS = 10


async def run_demo_scenarios(
    objectives: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
) -> list[ResearchRunResponse]:
    """Run a small bounded batch through the zero-network product path."""
    selected = list(DEFAULT_DEMO_OBJECTIVES if objectives is None else objectives)
    if not selected:
        raise ValueError("At least one demo objective is required")
    if len(selected) > MAX_DEMO_SCENARIOS:
        raise ValueError(f"Demo scenarios cannot exceed {MAX_DEMO_SCENARIOS}")

    application = ResearchApplication(settings or Settings(), max_sessions=len(selected))
    responses: list[ResearchRunResponse] = []
    for index, objective in enumerate(selected, start=1):
        submission = ResearchSubmission(
            objective=objective,
            metadata={"scenario": index, "mode": "offline_deterministic"},
        )
        responses.append(await application.submit(submission))
    return responses


def main() -> None:
    """Run default or user-supplied objectives and emit machine-readable JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--objective",
        action="append",
        dest="objectives",
        help="Research objective; repeat to run multiple bounded scenarios.",
    )
    args = parser.parse_args()
    responses = asyncio.run(run_demo_scenarios(args.objectives))
    payload = [response.model_dump(mode="json") for response in responses]
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
