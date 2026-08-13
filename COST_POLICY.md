# Cost Policy

## Overview
The V1 architecture must support controlled resource consumption to prevent runaway costs during autonomous research.

## Configurable Limits (implemented in Phase 6)
The following limits will be configurable via environment variables or configuration file:

### Research Iterations
- `MADRA_MAX_RESEARCH_ITERATIONS`: Maximum number of replanning cycles allowed (default: 3).

### Tool Calls
- `MADRA_MAX_TOOL_CALLS_PER_ITERATION`: Maximum tool calls per research iteration (default: 20).
- `MADRA_MAX_TOOL_CALLS_TOTAL`: Maximum tool calls per research session (default: 60).

### Model Calls
- `MADRA_MAX_MODEL_CALLS_PER_ITERATION`: Maximum LLM calls per iteration (default: 15).
- `MADRA_MAX_MODEL_CALLS_TOTAL`: Maximum LLM calls per research session (default: 45).

### Token Budgets (where measurable)
- `MADRA_MAX_TOKENS_PER_CALL`: Maximum tokens per LLM call (default: 4000).
- `MADRA_MAX_TOKENS_TOTAL`: Maximum tokens per research session (default: 50000).

### Time Budgets
- `MADRA_MAX_RESEARCH_TIME_SECONDS`: Maximum wall-clock time for a research session (default: 300 seconds = 5 minutes).
- `MADRA_MAX_TOOL_CALL_TIME_SECONDS`: Maximum time per tool call (default: 30 seconds).

### External API Limits
- `MADRA_MAX_EXTERNAL_API_CALLS`: Maximum calls to paid external APIs (e.g., search APIs) per session (default: 10).
- Per-API limits may be defined as needed.

### Retry Limits
- `MADRA_MAX_TOOL_RETRY_ATTEMPTS`: Maximum retry attempts for failed tool calls (default: 2).
- `MADRA_MAX_MODEL_RETRY_ATTEMPTS`: Maximum retry attempts for failed model calls (default: 2).

## Implementation Notes
- `RuntimeBudgetManager` enforces these limits before iterations and physical call attempts. Retries consume tool/model/external request budgets but do not consume research iterations.
- `MADRA_MAX_MODEL_CALL_TIME_SECONDS` defaults to 60 seconds to provide the model-call timeout required by the Phase 6 harness; tool timeout remains 30 seconds.
- Retry delays default to 0.1 seconds and double deterministically up to 2 seconds. Both values are configurable with `MADRA_RETRY_BACKOFF_SECONDS` and `MADRA_RETRY_BACKOFF_MAX_SECONDS`.
- The smallest of the operation timeout and remaining session time bounds each async call.

## Cost Tracking
- Runtime reports track research iterations, tool calls, model calls, paid external API calls, measurable tokens, elapsed time, and terminal failure metadata.
- Monetary estimation remains unavailable because the repository defines no authoritative provider pricing catalog.

## Emergency Stop
- `MADRA_EMERGENCY_STOP=true` halts a research run before its next bounded operation and produces a normalized terminal failure.

## Mandatory Validation Cost
- Phase 6 tests and the default API integration use deterministic fakes, require no network access or API credentials, and cost $0.
- Phase 7 end-to-end, adversarial, demo, security, performance-bound, and regression validation also uses deterministic local adapters and costs $0.

The offline demo accepts at most ten scenarios per invocation. Each scenario remains independently constrained by the Phase 6 runtime limits; demo batching does not replace or multiply hidden research loops.

## Responsible Use
- Users are encouraged to set reasonable limits based on their budget and research needs.
- The system defaults to conservative limits to prevent unexpected expenses.
