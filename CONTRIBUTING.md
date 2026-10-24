# Contributing

Contributions must keep the bounded, provider-neutral, evidence-first design described in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and the policies in [`docs/policies/`](docs/policies/).

## Local validation

Python 3.11, 3.12 or 3.13:

```bash
python -m pip install -e ".[test]"
pytest
ruff check .
mypy src
```

The test suite must stay deterministic: no live model or paid API calls, $0 to run. When you change a
boundary, add tests for success, failure, invalid input, exhaustion, state transitions and provenance.

Never commit `.env`, credentials, runtime databases, generated research, logs, caches or bytecode.
Use short-lived feature branches and Conventional Commit messages (`feat:`, `fix:`, `docs:`, `chore:`).
