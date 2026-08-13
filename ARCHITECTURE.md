# Architecture Overview

Based on the approved master architecture (see `docs/architecture/master_architecture.png`), this document describes the software architecture of the Multimodal Agentic Deep Research Assistant.

## Major Components

### 1. Master Research Orchestrator
- **Responsibility**: Coordinating the entire research workflow, delegating to specialized agents, and managing research state.
- **Boundaries**: Interfaces with Planning Agent, Research Agents, Analysis Agent, Evaluation Agent, and Report Agent.
- **Data Flow**: Receives user research question, outputs research plan, manages task delegation, synthesizes final report.

### 2. Planning Agent
- **Responsibility**: Decomposing research questions into actionable research tasks and creating research plans.
- **Boundaries**: Receives research question from Orchestrator, outputs structured research plan.

### 3. Research Agents (Specialized)
- **Responsibility**: Executing individual research tasks using appropriate tools (web search, document analysis, etc.).
- **Boundaries**: Interacts with Tool Registry, executes tool calls, returns raw findings and evidence.

### 4. Analysis/Synthesis Agent
- **Responsibility**: Extracting evidence from raw findings, analyzing information, and synthesizing insights.
- **Boundaries**: Receives raw research outputs, outputs analyzed evidence and claims.

### 5. Evaluation/Reflection Agent
- **Responsibility**: Evaluating research quality, checking for evidence gaps, and determining if replanning is needed.
- **Boundaries**: Receives synthesized analysis, outputs evaluation score and gap report; can trigger replanning.

### 6. Report Agent
- **Responsibility**: Generating final traceable report with citations from validated evidence and claims.
- **Boundaries**: Receives evaluated analysis, outputs final report in multiple formats.

### 7. Tool Registry
- **Responsibility**: Managing available research tools and enabling dynamic tool selection.
- **Boundaries**: Provides tool interfaces to Research Agents; tools include web search, document parsers, image/video analyzers, API clients.

### 8. Evidence Pipeline
- **Responsibility**: Normalizing evidence from diverse sources, establishing provenance, and supporting citation generation.
- **Boundaries**: Connects Research Agents to Analysis Agent; ensures evidence is traceable to source.

### 9. Context & Memory System
- **Responsibility**: Separating durable research memory, active `ResearchState`, and bounded per-agent working context.
- **Boundaries**: `ResearchSessionRepository` owns persistence; `BoundedResearchMemory` performs deterministic retrieval; `ResearchContextBuilder` selects a small traceable context that can be supplied to agents.

### 10. Model Gateway
- **Responsibility**: Abstracting interactions with various LLM providers (development and runtime).
- **Boundaries**: Used by all agents for LLM calls; provider-agnostic interface.

### 11. Observability & Logging
- **Responsibility**: Tracking agent actions, tool usage, token consumption, and system performance.
- **Boundaries**: Cross-cutting concern; integrated into all major components.

## Data Flow Summary
1. User submits research question to Orchestrator.
2. Orchestrator delegates to Planning Agent to create research plan.
3. Orchestrator delegates research tasks to specialized Research Agents.
4. Research Agents use tools to collect multimodal information, returning raw findings.
5. Analysis/Synthesis Agent processes findings to extract evidence and form claims.
6. Evaluation/Reflection Agent assesses quality and identifies gaps.
7. If gaps found, Orchestrator may replan; otherwise, proceeds to Report Agent.
8. Report Agent generates final report with citations from validated evidence.

## Phase 4 Research State Graph

The orchestrator enforces these legal transitions:

`initialized → planning → researching → analyzing → evaluating`

Evaluation then selects one of four bounded paths:

- `CONTINUE`: return to `planning` with normalized evidence gaps and retained reflection context.
- `COMPLETE`: transition through `reporting` to `completed` after the report is stored.
- `BLOCKED`: terminate as `blocked`.
- `FAILED`: terminate as `failed`.

If `CONTINUE` consumes the configured iteration limit, the orchestrator terminates as `failed` with an explicit bound-exhaustion error. `ResearchState` retains plan IDs, visited statuses, and evaluation records; Phase 5 can now checkpoint and reconstruct that state.

## Phase 5 Research Intelligence

Phase 5 preserves this dependency direction:

`domain models ← persistence protocol ← SQLite adapter`

`persistence protocol ← bounded research memory ← context builder ← agent workflow`

`ResearchSessionSnapshot` is the versioned recovery aggregate. It stores the request, active state, plans and tasks, sources, evidence, claims, task/evidence links, evaluation history, and report metadata. SQLite stores each aggregate atomically as canonical JSON and checks schema version `1` during initialization and reads. User-generated databases remain under the ignored `data/` runtime boundary.

Durable memory queries use deterministic lexical overlap. Results are relevance ordered with stable timestamp/UUID tie-breakers, capped by explicit query limits, and return evidence together with its source, supporting claims, task IDs, request ID, and session ID. Existing evidence, source, and claim objects cannot be removed or rewritten by a later snapshot.

The context builder does not invoke a model. It applies configured character, evidence, reflection, gap, task-ID, and candidate-session limits; excludes zero-relevance evidence for non-empty queries; truncates only the working excerpt; and retains the original evidence/source/claim identifiers plus a selection trace. Historical evidence is never changed by context compression.

The orchestrator accepts persistence and context dependencies optionally. When configured, it checkpoints bounded workflow state, supplies working context through backward-compatible optional agent parameters, and can reconstruct a saved session without starting a new loop. A normalized persistence failure terminates the run once as `failed`; Phase 5 adds no retry loop.

### Phase 5 Limitations

- SQLite is a synchronous local adapter, not a distributed or multi-process coordination service.
- Retrieval is lexical rather than embedding- or model-based, so semantic matches without shared terms are not selected.
- The context character limit is deterministic text sizing, not tokenizer-specific Phase 6 token accounting.
- Recovery reconstructs saved state but does not automatically resume a partially completed iteration.
- Legacy tool outputs containing only evidence/source IDs preserve those IDs, but content retrieval requires the corresponding domain objects to be stored in the session snapshot.

## Failure Boundaries
- Tool and model execution enter the Phase 6 harness before external work begins.
- Transient connection and timeout failures use bounded deterministic retry; permanent and unknown failures fail closed without retry.
- Ordered model fallback is provider neutral and occurs only after a route exhausts eligible transient retries.
- Evidence validation prevents propagation of unverified claims.
- Persistence failures are normalized and terminate the active run without retry recursion.
- Context selection and deterministic excerpt truncation prevent unbounded working context.

## Configuration
- Environment-based configuration for API keys, model endpoints, and system limits.
- Separate configuration for development vs. production.

## Phase 6 Reliability and Product Integration

Phase 6 adds this optional application-layer boundary without changing the Phase 4 state graph or Phase 5 context flow:

`orchestrator → execution harness → agent abstraction → provider/tool abstraction`

`settings → RuntimeLimits → RuntimeBudgetManager → RuntimeReport`

The harness owns a session-scoped `RuntimeBudgetManager`, retry policies, `asyncio` timeouts, sanitized `RuntimeEvent` emission, and normalized `RuntimeControlError` failures. Each physical retry is charged as a call. `begin_iteration()` is called exactly once per research iteration, so retry exhaustion cannot advance or recursively re-enter the research loop. The smaller of the operation timeout and remaining session time bounds every asynchronous call.

Planning, analysis, evaluation, and reporting calls are metered as model operations. Research-task calls are metered as tool operations; paid/network API accounting is activated explicitly in task metadata. Measurable provider `total_tokens` values are validated after responses. Budget, emergency-stop, timeout, transient, and permanent failures terminate once as `failed`, retain sanitized failure metadata, attach a runtime usage report, and remain compatible with SQLite checkpoints and recovery.

`RoutedModelGateway` depends only on `ModelGateway` routes and the harness. It retries a route according to the model policy, then advances in declared order only for transient or timeout exhaustion. It never imports provider SDKs.

The product boundary is FastAPI: `POST /research` starts a bounded deterministic run and `GET /research/{session_id}` reads the process-local terminal result. No graphical UI was defined by the repository contract. Durable product deployments can inject the existing repository and context abstractions; the default API makes no network or paid call.

### Phase 6 Limitations

- Process-local API lookup retains at most 100 insertion-ordered sessions; it is not a distributed job queue or multi-worker status store.
- `asyncio` timeouts require asynchronous providers to honor cancellation; blocking SDKs need an adapter-level thread/process boundary.
- Token enforcement is exact only when a provider reports `total_tokens`; agent calls without usage data remain request-count and time bounded.
- Provider price catalogs and monetary cost estimation are not implemented because no authoritative pricing source is defined.
- Automatic continuation of a partially completed recovered iteration remains unsupported.

## Phase 7 Final Integration

Phase 7 does not change the frozen state graph or introduce a new provider boundary. It validates the composed path:

`validated request → orchestrator → bounded context → execution harness → deterministic agents/tools → report/runtime metadata → SQLite checkpoint/recovery`

API metadata has independent item/key/value bounds and rejects secret-like fields before orchestration. A shared data-safety helper reports only the offending structural path; SQLite uses the same helper before serialization. Tool exceptions are normalized to a stable public message plus exception type and failure category, never the raw exception message.

The offline demo composes `ResearchApplication` and existing deterministic agents. Its ten-scenario maximum is separate from research-iteration, retry, fallback, and recovery bounds. GitHub Actions reproduces the repository's pytest, Ruff, and strict MyPy gates on Python 3.12.

### Phase 7 Limitations

- The demo is synthetic/offline; its web and document tool implementations are deterministic fakes, not live retrieval.
- The default full-stack deterministic run persists stable evidence/source identifiers but not full evidence/source objects because Phase 3 legacy tool output supplies identifiers only.
- Live provider, paid API, and non-text media processing remain deferred and unverified.
- The frozen roadmap defines no Phase 8 scope.
