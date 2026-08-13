# Decisions

## Phase 0 Decisions

### Decision 001: Project Structure
- **Date**: 2026-08-11
- **Decision**: Use a `src` layout with the package name `multimodal_agentic_deep_research_assistant`.
- **Rationale**: Standard Python packaging practice; clear separation of source and tests/documentation.

### Decision 002: Documentation Authority
- **Date**: 2026-08-11
- **Decision**: The following documents are authoritative for reconstructing project state:
  PROJECT_RULES.md, ROADMAP.md, ARCHITECTURE.md, PROJECT_STATUS.md, TASKS.md, DECISIONS.md, TESTING_STRATEGY.md, MODEL_POLICY.md, COST_POLICY.md, EVIDENCE_POLICY.md, CITATION_POLICY.md, SECURITY_GUIDELINES.md, GIT_WORKFLOW.md.
- **Rationale**: Ensures future AI agents (and humans) can understand project constraints and progress.

### Decision 003: Engineering Methodologies
- **Date**: 2026-08-11
- **Decision**: Adopt Context Engineering, Loop Engineering, Harness Engineering, Graph/Dependency Engineering, Evaluation Engineering, and Evidence-Driven Development as core methodologies.
- **Rationale**: These methodologies provide disciplined, scalable, and verifiable development practices suited to agentic AI systems.

### Decision 004: Technology Stack
- **Date**: 2026-08-11
- **Decision**: Primary language: Python 3.12+. Web framework: FastAPI (for API layer). Configuration: Pydantic + environment variables. Testing: pytest. Linting: Ruff. Type checking: mypy.
- **Rationale**: Modern, well-supported, and appropriate for the project scope without overengineering.

### Decision 005: Model Abstraction
- **Date**: 2026-08-11
- **Decision**: Design a provider-neutral model gateway to decouple the research engine from specific LLM providers.
- **Rationale**: Allows flexibility in model choice and prevents vendor lock-in.

## Future Phases
[TBD]

## Phase 4 Decisions

### Decision 006: Explicit Research State Graph
- **Date**: 2026-08-13
- **Decision**: Enforce legal workflow transitions in the orchestrator and record every visited state.
- **Rationale**: Makes loop behavior deterministic, inspectable, and testable while preventing accidental terminal-state regressions.

### Decision 007: Evaluation Feedback Is Loop State
- **Date**: 2026-08-13
- **Decision**: Retain the latest evaluation feedback for replanning and an ordered record of every evaluation for reflection evidence.
- **Rationale**: Replanning requires current gaps, while auditability requires the prior records not be overwritten.

### Decision 008: Bounded Deterministic Replanning
- **Date**: 2026-08-13
- **Decision**: Build follow-up plans from normalized, unique evaluation gaps and enforce a positive iteration limit.
- **Rationale**: The research loop must make targeted progress and terminate predictably without introducing Phase 5 memory or Phase 6 retry/budget systems.

## Phase 5 Decisions

### Decision 009: Standard-Library SQLite Local Adapter
- **Date**: 2026-08-14
- **Decision**: Use SQLite behind a `ResearchSessionRepository` protocol as the Phase 5 local persistence adapter.
- **Rationale**: The repository did not mandate a database. SQLite provides atomic local writes, deterministic initialization, and zero-network tests without adding infrastructure or dependencies.

### Decision 010: Versioned Session Recovery Aggregate
- **Date**: 2026-08-14
- **Decision**: Persist a schema-versioned `ResearchSessionSnapshot` containing the request, active state, plan/task history, evidence graph, evaluation history, and report metadata.
- **Rationale**: An aggregate transaction prevents partial cross-entity checkpoints while retaining stable IDs and complete supported recovery state. Existing source, evidence, and claim objects are immutable across updates.

### Decision 011: Deterministic Bounded Context Selection
- **Date**: 2026-08-14
- **Decision**: Select working context with lexical relevance, stable tie-breakers, explicit item/character limits, and provenance-preserving evidence excerpts.
- **Rationale**: Context construction remains reproducible, testable, provider-independent, and free of LLM cost. This is a context-size mechanism, not the Phase 6 runtime token-budget system.

### Decision 012: Optional Fail-Closed Orchestrator Integration
- **Date**: 2026-08-14
- **Decision**: Inject persistence and context services optionally; checkpoint configured runs, expose session reconstruction, and convert normalized persistence failure into one terminal failure without retry.
- **Rationale**: Existing Phase 0–4 behavior and callers remain compatible while configured workflows gain recovery and bounded agent context without introducing Phase 6 retry infrastructure.
