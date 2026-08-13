# Multimodal Agentic Deep Research Assistant

A provider-neutral Python research engine for bounded, evidence-oriented, multimodal research workflows.

## Project Status

Phase 5 — Research Intelligence is complete. Phase 6 has not started.

Implemented capabilities include:

- Core research, plan, task, state, evidence, source, claim, and citation contracts
- Dependency-injected planning, research, analysis, evaluation, and reporting agents
- Multimodal tool registry and deterministic web/document research tools
- Explicit bounded research state graph and terminal-state handling
- Evidence-gap detection, reflection history, and deterministic replanning
- Provider-independent research-session persistence contracts
- Transactional local SQLite persistence with schema-version checks and recovery
- Bounded research memory with deterministic evidence retrieval and provenance
- Reproducible context construction with character, evidence, reflection, and task limits
- Optional orchestrator checkpoints, session recovery, and agent working-context injection
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
- `src/deep_research/persistence/` — persistence contracts and SQLite adapter
- `src/deep_research/memory/` — bounded durable research memory
- `src/deep_research/context/` — deterministic agent-context construction
- `tests/` — automated tests
- `docs/architecture/` — architecture assets
- Root Markdown files — authoritative project policies, decisions, roadmap, and status

## Phase Discipline

See `ROADMAP.md` and `PROJECT_RULES.md`. Phase 6 reliability and product-integration functionality remains intentionally out of scope.
