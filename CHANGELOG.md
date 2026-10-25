# Changelog

## [Unreleased] - feat/phase6

### Changed
- **Step-by-step approval** ("Asks for your OK before each important step") is now one setting, off by default:
  the toggle in Settings -> Approval policy is saved by the control API in `control_settings.json` (survives
  restarts), New Research has a per-run checkbox that defaults to it, and the run page shows the state.
  Initial default: `DEEP_RESEARCH_APPROVAL_MODE` from the environment or `.env` (precedence: saved value, then env/.env, then off).

### Fixed
- `control_settings.json` holding valid non-object JSON (`42`, `true`, `null`, `[]`, a string) no longer causes a 500; it is treated as empty. `approval_mode` / `auto_approve_low_risk` are parsed strictly: JSON booleans, or the strings true/false (also 1/0, yes/no, on/off, any case); anything else falls back to env/.env (approval) or off.

### Removed
- The General-tab toggle "Step-by-step approval on by default". It was a dashboard-only preference that defaulted
  to ON. Its `step_approval` key in the dashboard preferences file is now ignored (left in place, not migrated);
  use the Settings toggle instead.

## [1.0.0] - 2026-10-24

### Added
- LLM planning and evaluation agents, each with a deterministic fallback for every decision. Added an optional human-in-the-loop step gate.
- A supervised-control API (`deep-research-control`, 127.0.0.1 only, bearer token). It covers run lifecycle, step approve, reject and retry, an audit log, and write-only masked keys.
- A plain-text log format, secret redaction, safe 422 responses, HTTP and crash context in logs, and research lifecycle events.
- ZetaCell demo data with 24 ready-made requests (research, control and error cases).

### Changed
- Offline report titles now use the research objective instead of the request UUID.
- Version 1.0.0. The LICENSE and package author are now Attota Leelaissak. `DEEP_RESEARCH_CONTROL_TOKEN` is documented in `.env.example`.
- Policies, decisions, architecture and the testing strategy moved under `docs/`. Internal phase notes moved out of the repo. Screenshots were refreshed under `docs/images/`. The live-mode reference moved to `docs/LIVE_MODE.md`, and the README was rewritten.

## [Unreleased before 1.0.0]

### Changed (breaking)
- The old project acronym is gone everywhere. The settings env prefix is now `DEEP_RESEARCH_`, so rename the variables in an existing `.env`. The free-model cache dir is now `~/.cache/deep_research/` (`%LOCALAPPDATA%\deep_research\` on Windows). The demo command is `deep-research-demo`, the Docker image is `deep-research`, and the container user is `appuser`.
- Added `docs/SETUP_WINDOWS.md`.

### Fixed / improved (full polish)
- The free-model selection now persists across restarts. On first run the app discovers the OpenRouter `:free` models and probes the top-ranked text and vision models concurrently (tiny prompt, short timeout). If no model of a kind works, it probes the next batch, up to 3 batches. The fastest working model per kind is selected, and the selection, ranked lists, probe results and a timestamp are saved as JSON in the per-user cache dir (`DEEP_RESEARCH_OPENROUTER_MODEL_CACHE_PATH`, `off` to disable). The API key is never written. The file is reused for 6h; after expiry the app re-discovers and re-probes automatically.
- Fallback now also covers 200 responses with an error body, and null or empty content (reasoning models that run out of budget). Failed models are demoted.
- Attached PDFs and images are processed before web tasks. Their text is split into page chunks, and the most relevant chunks are fed to every section's synthesis as primary sources, numbered ahead of web results and cited as `user attachment`. Sections still complete from the attachments alone if web search is blocked.
- Scanned or image-only PDFs no longer fail the run. The result carries the note `no extractable text`, the page count, metadata and an embedded-image inventory, plus free-vision descriptions of the first page's images when a vision model is available.
- Report headings are concise, keyword-based sections (Fundamentals / Benefits / Risks / ...) instead of the repeated question. Each section has an aspect-scoped search query and LLM focus. Full-width `【n】` citations and exotic spaces are normalised.

### Fixed (live mode, real world)
- Automatic runtime free-model selection: OpenRouter `/models` catalog (6h cache, refresh on failure), `:free` filtering, separate text/vision ranking, demotion of failed models, fallback on 403/404/408/429/5xx. `DEEP_RESEARCH_OPENROUTER_MODEL=auto` is the default; pinned `:free` ids are tried first.
- Web search: DuckDuckGo 202/challenge pages count as failures. Search now tries a no-key chain automatically: Tavily (only with a key) → DDG HTML → DDG Lite → Wikipedia full-text → DDG Instant Answer. The planner no longer adds a 'latest' prefix.
- Live reports now include the LLM syntheses with report-wide `[n]` citations that link to real source URLs, plus `models_used`.

### Fixed (post-Phase 7)
- Restored modules imported by `provider_wiring.py` but never committed (`core/free_model_guard.py`, `models/guarded_gateway.py`, `models/providers/openrouter_gateway.py`); `audio_transcription_tool` and `provider_wiring` previously failed with `ModuleNotFoundError`
- Added a `transcribe()` interface on `ModelGateway` (fails closed by default) and `ModelRequest.audio_data`; added provider credential settings; added `httpx` as a runtime dependency
- Replaced the bare `NotImplementedError` in `STTProvider.transcribe` with a structured `ModelError` so the router falls back cleanly
- MyPy strict passes again (the 26 errors left by "fix: resolve pre-existing mypy typing errors" are resolved)
- Removed the Starlette/httpx `TestClient` deprecation warning by testing the API through `httpx.ASGITransport`

### Added (post-Phase 7)
- Offline tests for the free STT router, providers, guard, guarded gateway, wiring and `AudioTranscriptionTool`
- Live research mode (`DEEP_RESEARCH_RESEARCH_MODE=live`, default `offline`): `LiveResearchAgent` with DuckDuckGo HTML (free) or Tavily search, SSRF-guarded size-capped page fetch, and cited LLM synthesis through the free-model-guarded OpenRouter gateway with snippet fallback
- `PdfReaderTool` (pypdf) and `ImageAnalysisTool` (Pillow + optional `:free` OpenRouter vision description) with size/page/pixel limits and safe errors; research submissions accept up to 5 PDF/image `attachments`, each routed to its own task
- Python 3.11/3.12/3.13 support and CI matrix; Dockerfile and `.dockerignore` (uvicorn, non-root, healthcheck)

### Removed
- Duplicate root-level `master_architecture.png.png` (identical to `docs/architecture/master_architecture.png`)

### Added
- Phase 7 full-stack offline integration coverage across orchestration, persistence, recovery, context construction, runtime controls, reporting, and provenance identifiers
- Adversarial tests for bounded input, secret-like metadata, credential-bearing URLs, sanitized tool failures, bounded session retention, and demo-batch exhaustion
- Shared application/infrastructure sensitive-data path detection that never returns discovered values
- Installable `deep-research-demo` command with bounded zero-network release scenarios
- GitHub Actions pytest/Ruff/MyPy quality gate, MIT license, contribution guide, security-reporting policy, and resume-ready project entry
- Phase 6 session-scoped execution harness with explicit retry, timeout, budget, emergency-stop, and normalized-failure contracts
- Deterministic exponential backoff, bounded transient retries, and independent retry/research-iteration accounting
- Tool/model request limits, measurable token limits, external-API limits, and wall-clock enforcement
- Provider-neutral ordered model routing with transient timeout/connection fallback
- Sanitized runtime event observation plus persistence-safe usage and failure reports
- Bounded research submission and result lookup FastAPI endpoints
- Phase 6 deterministic tests for success, failures, exhaustion, timeouts, budgets, routing, persistence, context, provenance, and API integration
- Phase 5 provider-independent persistence and versioned research-session snapshots
- Transactional standard-library SQLite adapter with deterministic schema initialization
- Bounded durable research memory with lexical relevance ordering and full provenance
- Deterministic context builder with explicit character and item limits and selection traces
- Optional orchestrator checkpoints, context-aware agent calls, and session reconstruction
- Phase 5 tests for CRUD, round trips, provenance, immutable history, secret rejection, retrieval quality, context limits, recovery, and persistence failures
- Phase 4 bounded research state graph with validated transitions and terminal states
- Per-iteration evaluation/reflection history with normalized evidence gaps
- Deterministic gap-driven replanning and executable research tasks
- Phase 4 workflow coverage for completion, replanning, blocking, failure, and iteration exhaustion
- Phase 1 core foundation: configuration, domain contracts, API foundation, testing foundation
- Implemented FastAPI application with health, ready, and version endpoints
- Added comprehensive test suite (unit tests for configuration, evidence, research requests, tasks, and API endpoints)
- Fixed all Ruff linting errors
- Fixed all mypy typing errors
- Added .env.example with placeholders
- Ensured .env is ignored by .gitignore
- Synchronized documentation (README, PROJECT_STATUS, TASKS, DECISIONS)
- Verified no secrets committed, no external service required for Phase 1
- Initial project structure
- Master architecture image copied to docs/
- Authoritative documentation files (README, PROJECT_RULES, ROADMAP, ARCHITECTURE, PROJECT_STATUS, TASKS, DECISIONS)
- Phase 0 planning documents

### Changed
- Phase 7 metadata validation now forbids unknown fields, null objectives, more than 50 metadata items, long keys/values, non-finite numbers, and secret-like field names
- Domain timestamps now default to timezone-aware UTC values and legacy Pydantic class configuration was removed
- Citation identifiers now reject blank values
- Project packaging now uses one authoritative `pyproject.toml` configuration and the real repository URLs
- Orchestrator agent invocations optionally pass through the Phase 6 harness while preserving legacy direct invocation
- Deterministic web-search output now supplies stable evidence/source identifiers for the zero-network product API
- Environment settings now expose every repository-defined Phase 6 cost and execution control
- Research agent contracts accept an optional bounded `AgentContext` while retaining existing call behavior
- Repeated evidence, source, and task identifiers are deduplicated before entering active state
- Successful research runs now become `completed` after report generation
- Orchestration accepts both mapping-based and typed Pydantic tool outputs from Phase 3
- Evaluation decisions and confidence values are validated at their contract boundary
### Fixed
- Deterministic tool failures no longer include raw exception messages in result/error surfaces
- Removed project-owned Pydantic V2 and naive-UTC deprecation warnings from the validation suite
- Mutable model/tool request defaults now use independent factories
- Persistence failures now terminate a configured workflow once instead of escaping into an unbounded research path
- Corrected mutable-state test captures that obscured iteration-specific feedback
- Removed an unavailable analysis tool assignment from deterministic research plans
