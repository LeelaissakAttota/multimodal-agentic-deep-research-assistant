# Project Rules

1. **Phase Discipline** - Strictly adhere to phase boundaries; do not implement future phase functionality.
2. **Evidence-First** - All claims must be traceable to supporting evidence; never fabricate sources, citations, or findings.
3. **Context Engineering** - Load only necessary context for current task; preserve decisions in authoritative documents.
4. **Loop Engineering** - Use bounded INSPECT→UNDERSTAND→PLAN→IMPLEMENT→TEST→EVALUATE loops with diagnosis/fix on failure.
5. **Harness Engineering** - Operate within strict development harness with explicit scope, validation, testing, and safety.
6. **Graph/Dependency Engineering** - Implement components in dependency order; higher-level requires lower-level contracts.
7. **Evaluation Engineering** - All phase completion requires measurable acceptance criteria (tests, lint, type checks, etc.).
8. **Separation of Concerns** - Maintain clear boundaries between research planning, execution, analysis, evaluation, and reporting.
9. **Model Neutrality** - Design provider-neutral model gateway; no hardcoded API keys or model coupling.
10. **Security Baseline** - Use environment variables for secrets; validate inputs; safe logging and file handling.
