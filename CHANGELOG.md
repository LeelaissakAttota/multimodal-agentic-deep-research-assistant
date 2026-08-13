# Changelog

## [Unreleased]
### Added
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
- Research agent contracts accept an optional bounded `AgentContext` while retaining existing call behavior
- Repeated evidence, source, and task identifiers are deduplicated before entering active state
- Successful research runs now become `completed` after report generation
- Orchestration accepts both mapping-based and typed Pydantic tool outputs from Phase 3
- Evaluation decisions and confidence values are validated at their contract boundary
### Fixed
- Persistence failures now terminate a configured workflow once instead of escaping into an unbounded research path
- Corrected mutable-state test captures that obscured iteration-specific feedback
- Removed an unavailable analysis tool assignment from deterministic research plans
