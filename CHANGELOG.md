# Changelog

## [Unreleased]
### Added
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
- Successful research runs now become `completed` after report generation
- Orchestration accepts both mapping-based and typed Pydantic tool outputs from Phase 3
- Evaluation decisions and confidence values are validated at their contract boundary
### Fixed
- Corrected mutable-state test captures that obscured iteration-specific feedback
- Removed an unavailable analysis tool assignment from deterministic research plans
