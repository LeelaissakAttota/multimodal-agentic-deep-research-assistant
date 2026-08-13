# Resume Project Entry

## Multimodal Agentic Deep Research Assistant

Architected and delivered a provider-neutral Python 3.12 research engine that coordinates planning, tool use, synthesis, evaluation, bounded replanning, and citation-aware reporting through an explicit state graph. Implemented deterministic multimodal tool selection, provenance-preserving evidence contracts, transactional SQLite recovery, bounded context construction, runtime retries/timeouts/budgets, model fallback, sanitized observability, and FastAPI product endpoints.

Engineered finite research, retry, fallback, and recovery loops with independent accounting and fail-closed terminal behavior. Added end-to-end, adversarial, persistence, context, provenance, performance-bound, security, and regression validation using offline fakes at $0 mandatory test cost, plus Ruff, strict MyPy, and GitHub Actions quality gates.

Technology: Python, FastAPI, Pydantic, asyncio, SQLite, pytest, Ruff, MyPy, GitHub Actions.

Scope note: the released demo is deterministic and offline. Live provider SDKs, paid API verification, graphical UI, and automatic continuation of partially completed recovered iterations are not claimed.
