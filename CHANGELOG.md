# Changelog

## [Unreleased]
### Added
- Phase 7 full-stack offline integration coverage across orchestration, persistence, recovery, context construction, runtime controls, reporting, and provenance identifiers
- Adversarial tests for bounded input, secret-like metadata, credential-bearing URLs, sanitized tool failures, bounded session retention, and demo-batch exhaustion
- Shared application/infrastructure sensitive-data path detection that never returns discovered values
- Installable `madra-demo` command with bounded zero-network release scenarios
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
