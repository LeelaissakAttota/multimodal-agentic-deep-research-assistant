# Multimodal Agentic Deep Research Assistant

A provider-neutral Python research engine for bounded, evidence-oriented, multimodal research workflows.

## Project Status

Phase 4 — Deep Research Workflow is complete. Phase 5 has not started.

Implemented capabilities include:

- Core research, plan, task, state, evidence, source, claim, and citation contracts
- Dependency-injected planning, research, analysis, evaluation, and reporting agents
- Multimodal tool registry and deterministic web/document research tools
- Explicit bounded research state graph and terminal-state handling
- Evidence-gap detection, reflection history, and deterministic replanning
- Mapping-based and typed Pydantic tool-output compatibility
- FastAPI health, readiness, and version endpoints

## Validation

```powershell
pytest -q
ruff check .
mypy src/deep_research
```

## Repository Structure

- `src/deep_research/` — application source
- `tests/` — automated tests
- `docs/architecture/` — architecture assets
- Root Markdown files — authoritative project policies, decisions, roadmap, and status

## Phase Discipline

See `ROADMAP.md` and `PROJECT_RULES.md`. Phase 5 functionality is intentionally excluded from the Phase 4 release.
