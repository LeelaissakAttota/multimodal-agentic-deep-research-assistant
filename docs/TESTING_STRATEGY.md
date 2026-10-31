# Testing Strategy

## Overview
Testing is integral to the development harness. Every phase must have measurable acceptance criteria.

## Test Types

### Unit Tests
- **Purpose**: Validate individual functions and classes in isolation.
- **Framework**: pytest
- **Coverage**: Aim for high coverage on core domain logic and utilities.
- **Location**: `tests/unit/`

### Integration Tests
- **Purpose**: Validate interactions between components (e.g., orchestrator and planning agent).
- **Framework**: pytest
- **Location**: `tests/integration/`

### Contract Tests
- **Purpose**: Validate that components adhere to defined interfaces (especially important for agent contracts and tool interfaces).
- **Framework**: pytest with custom contract assertions
- **Location**: `tests/contract/`

### Evaluation Tests
- **Purpose**: Validate evaluation logic, evidence gap detection, and reflection mechanisms.
- **Framework**: pytest
- **Location**: `tests/evaluation/`

### Workflow Tests
- **Purpose**: Validate end-to-end research workflows (without external API calls where possible).
- **Framework**: pytest with mocks
- **Location**: To be created in later phases.

### Research Intelligence Tests
- **Purpose**: Validate Phase 5 persistence, recovery, memory retrieval, provenance, and bounded context construction.
- **Method**: Standard-library SQLite databases in the gitignored `data/phase5-tests/` runtime area, deterministic timestamps/UUIDs, and injected agent doubles.
- **Network/Cost**: No network access, paid API, or model call is permitted.

### Reliability and Product Integration Tests
- **Purpose**: Validate Phase 6 retries, timeouts, budgets, fallback routing, observable failures, terminal behavior, recovery, and API boundaries.
- **Method**: Deterministic async fakes, injected sleepers/clocks where applicable, provider-neutral gateway doubles, local ignored SQLite files, and FastAPI's in-process test client.
- **Mandatory paths**: Success; transient retry success; transient exhaustion; permanent failure; timeout; tool/model/token/external-API/emergency budget exhaustion; invalid policy; eligible and ineligible model fallback; persisted failure/report recovery; bounded context/provenance retention; API validation and lookup.
- **Network/Cost**: Mandatory tests and the default product API use fake providers/tools only and cost $0.

### Final Integration Tests
- **Purpose**: Validate the released Phase 7 composition and its adversarial, security, performance-bound, and packaging contracts.
- **Location**: `tests/integration/test_phase7_final_integration.py`, with regression coverage retained in `tests/unit/`.
- **Method**: Compose deterministic agents, SQLite persistence, bounded memory/context, the execution harness, reports, recovery, and the product service in one offline workflow. Exercise malicious/oversized input, secret-like fields, credential-bearing URLs, raw exception messages, bounded registries, and bounded demo batches.
- **Performance method**: Verify explicit cardinality/resource bounds in tests and record repeatable local wall-clock observations in the Phase 7 audit; do not use brittle machine-specific pass/fail timing thresholds.
- **Network/Cost**: No live network, provider credential, paid API, or model call is permitted. Mandatory cost is $0.

### Regression Tests
- **Purpose**: Ensure previously fixed issues do not reappear.
- **Framework**: pytest (reuse unit/integration tests)
- **Location**: Same as unit/integration.

### Static Analysis
- **Tools**: Ruff (linting), mypy (type checking)
- **Frequency**: Run on every change; enforced by pre-commit hooks (to be set up).

### Architecture Validation
- **Purpose**: Ensure implementation adheres to documented architecture boundaries.
- **Method**: Periodic review; automated checks where feasible (e.g., dependency analysis).

### Failure Path Testing
- **Purpose**: Test system behavior under failure conditions (tool failures, model timeouts, invalid inputs).
- **Method**: Unit and integration tests with mock failures.

## Test Data
- Use fixtures for sample research questions, expected plans, and mock tool responses.
- Avoid real API calls in unit tests; use mocks or recorded responses (VCR-style where appropriate).

## Coverage Goals
- Phase 0: N/A (no functional code)
- Phase 1: 80%+ on core domain contracts
- Phase 2: 70%+ on orchestration logic
- Phase 3-7: 60%+ overall, with focus on critical paths
- Phase 5: persistence round trips, immutable evidence history, retrieval relevance/order/limits, context budgets, recovery, and failure boundaries are mandatory
- Phase 6: all finite-execution controls and failure categories, routing/fallback, runtime reports, API integration, and Phase 0–5 regressions are mandatory
- Phase 7: composed end-to-end success, adversarial input, sensitive-data rejection, failure sanitization, bounded batches, recovery, context, provenance, runtime accounting, and Phase 0–6 regressions are mandatory

## Test Execution
- Local development: `pytest`
- CI: `.github/workflows/quality.yml` runs pytest, Ruff, and strict MyPy on Python 3.12 for pushes and pull requests to `main`.

## Test Environment
- Isolated environments using virtualenv or similar.
- Test doubles (mocks, fakes) for external dependencies (web search APIs, file systems, etc.).
