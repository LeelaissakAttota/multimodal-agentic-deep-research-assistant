# Security Policy

## Reporting a vulnerability

Use GitHub's private vulnerability-reporting or security-advisory flow for this repository when available. Do not place credentials, exploit payloads containing real secrets, or sensitive research data in a public issue. If a credential may have been exposed, revoke and rotate it before sharing a sanitized report.

Include the affected version or commit, the impacted boundary, reproduction steps using synthetic data, and the expected security property. See [`docs/SECURITY_GUIDELINES.md`](docs/SECURITY_GUIDELINES.md) for the project's threat controls and secure-development requirements.

## Supported release

The current `main` branch (v1.0.x) is supported. Live mode uses only free OpenRouter models and free search sources, and only when you opt in with `DEEP_RESEARCH_RESEARCH_MODE=live`. The test suite and the bundled demo make no network or paid-model calls. Logs redact keys and tokens, and the control API listens on 127.0.0.1 only and requires a bearer token.
