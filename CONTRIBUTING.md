# Contributing

Contributions must preserve the bounded, provider-neutral, evidence-first architecture described in `ARCHITECTURE.md` and `PROJECT_RULES.md`.

## Local validation

Use Python 3.12 and install the test extra:

```powershell
python -m pip install -e ".[test]"
pytest
ruff check .
mypy src
```

Mandatory tests must remain deterministic, make no live model or paid API calls, and cost $0. Add contract-level coverage for success, failure, invalid input, exhaustion, state transitions, provenance, and compatibility when changing a boundary.

Never commit `.env`, credentials, runtime databases, generated research, logs, caches, or bytecode. Follow `GIT_WORKFLOW.md` for branch and commit conventions and update the authoritative project documents when behavior changes.
