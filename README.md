# Multimodal Agentic Deep Research Assistant

A provider-neutral Python research engine for bounded, evidence-oriented, multimodal research workflows.

## Project Status

Phase 6 — Reliability and Product Integration is complete. Phase 7 has not started.

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
- Session-scoped execution harness with bounded retries, timeouts, and resource budgets
- Independent retry and research-iteration accounting with deterministic exponential backoff
- Provider-neutral ordered model routing and transient-failure fallback
- Normalized terminal failures, sanitized runtime events, and persisted usage reports
- Emergency stop plus tool, model, token, external-API, and wall-clock limits
- Bounded `POST /research` execution and `GET /research/{session_id}` status/report APIs
- Mapping-based and typed Pydantic tool-output compatibility
- FastAPI health, readiness, and version endpoints

## Validation

```powershell
pytest -q
ruff check .
mypy src
```

## Repository Structure

- `src/deep_research/` — application source
- `src/deep_research/persistence/` — persistence contracts and SQLite adapter
- `src/deep_research/memory/` — bounded durable research memory
- `src/deep_research/context/` — deterministic agent-context construction
- `src/deep_research/runtime/` — retry, timeout, budget, failure, and telemetry controls
- `tests/` — automated tests
- `docs/architecture/` — architecture assets
- Root Markdown files — authoritative project policies, decisions, roadmap, and status

## Phase Discipline

See `ROADMAP.md` and `PROJECT_RULES.md`. Phase 7 adversarial testing, stabilization, performance review, demo scenarios, and final packaging remain intentionally out of scope.

## Phase 6 Runtime Defaults

Runtime controls are configured with `MADRA_` environment variables. Defaults are: 3 research iterations; 20/60 tool calls per iteration/session; 15/45 model calls per iteration/session; 4,000/50,000 measurable tokens per call/session; 300 seconds per session; 30 seconds per tool call; 60 seconds per model call; 10 paid external API calls; and 2 retries for each tool or model operation. `MADRA_EMERGENCY_STOP=true` halts a run before its next bounded operation.

Retries consume call budgets but never consume research iterations. Unknown exceptions and explicitly permanent failures are not retried. Only transient connection/timeout failures are eligible for ordered model fallback.

## Product API

`POST /research` accepts a validated objective and executes the deterministic, zero-network product integration. `GET /research/{session_id}` returns its terminal report and runtime accounting. The API registry is process-local and retains at most 100 sessions; applications that require durable or multi-worker lookup must compose the existing `ResearchSessionRepository` boundary.
