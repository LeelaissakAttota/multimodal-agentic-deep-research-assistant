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
