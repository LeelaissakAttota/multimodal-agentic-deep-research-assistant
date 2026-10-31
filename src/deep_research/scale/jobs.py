"""Job functions executed by queue workers (importable by dotted path)."""

from __future__ import annotations

from typing import Any


def run_research_job(payload: dict[str, Any]) -> dict[str, Any]:
    """Run one research request headless (no approval gate) and return the JSON response.

    ``payload`` = ``{"objective": str, "mode": "offline"|"live"|None, "attachments": [...]}``. It never
    contains keys: the worker reads API keys from its own secret store / environment.
    """
    import asyncio

    from deep_research.api.research_service import ResearchApplication, ResearchSubmission
    from deep_research.control.service import DeepResearchControl, _attachments, run_variant
    from deep_research.core.config import Settings

    objective = str(payload.get("objective") or "").strip()
    if not objective:
        raise ValueError("objective is required")
    mode = payload.get("mode")
    control = DeepResearchControl(Settings(), with_queue=False)
    settings = control.settings_with_keys(mode if isinstance(mode, str) else None)
    run_id = str(payload.get("run_id") or "")
    assignment = None
    if run_id:
        settings, assignment = run_variant(settings, run_id)
    application = ResearchApplication(settings)
    response = asyncio.run(application.submit(
        ResearchSubmission(objective=objective, attachments=_attachments(payload.get("attachments"))),
        run_id=run_id or None))
    result = response.model_dump(mode="json")
    if run_id:
        from deep_research.scale.graphrag import auto_sync

        auto_sync(run_id, result, objective[:200])
    if assignment:
        result["experiment"] = assignment
    return result
