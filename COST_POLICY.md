# Cost Policy

## Overview
The V1 architecture must support controlled resource consumption to prevent runaway costs during autonomous research.

## Configurable Limits (to be implemented)
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
- These limits are **not** implemented in Phase 0; they are specified for future implementation.
- The architecture will include a `BudgetManager` or similar component to enforce limits.
- Limits will be checked at appropriate points (e.g., before each tool call, model call, iteration).

## Cost Tracking
- The system will track and report estimated costs (where pricing is known) for transparency.
- Actual cost calculation depends on provider pricing and is deferred to later phases.

## Emergency Stop
- A manual override (e.g., environment variable `MADRA_EMERGENCY_STOP=true`) will halt all research operations immediately.

## Responsible Use
- Users are encouraged to set reasonable limits based on their budget and research needs.
- The system defaults to conservative limits to prevent unexpected expenses.
