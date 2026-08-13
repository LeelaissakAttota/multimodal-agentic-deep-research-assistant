# Security Guidelines

## Secrets Management
- All secrets (API keys, tokens, etc.) must be stored in environment variables.
- Never hardcode secrets in source code.
- Use `.env` file for local development (excluded from Git via .gitignore).
- In production, use secure secret management (e.g., HashiCorp Vault, AWS Secrets Manager).
- Phase 5 persistence rejects secret-like metadata keys and URLs containing user information before writing a session payload.
- Phase 7 centralizes this detection so API and persistence checks use the same recursive key/credential-URL rules and expose only the offending path, never the value.

## Environment Variables
- Prefix environment variables with `MADRA_` (Multimodal Agentic Deep Research Assistant) to avoid collisions.
- Example: `MADRA_OPENAI_API_KEY`, `MADRA_GOOGLE_SEARCH_API_KEY`.

## Input Validation
- Validate all external inputs (user queries, API responses, file uploads).
- Use allowlists where possible (e.g., allowed URL schemes, file types).
- Reject or sanitize inputs that do not conform to expectations.
- Phase 7 API submissions forbid unknown fields; bound objective length, metadata count, key length, and string-value length; reject null characters, non-finite numbers, and secret-like metadata names.

## Safe File Handling
- Restrict file operations to designated directories (e.g., `data/`, `reports/`).
- Validate file paths to prevent directory traversal attacks.
- Impose size limits on uploads and downloads.
- Local research databases are runtime data under `data/`, which is gitignored; database files, journals, and recovered user research state must not be committed.

## URL Validation
- Validate URLs for allowed schemes (http, https) and block dangerous schemes (file, gopher, etc.).
- Consider using allowlists for trusted domains in research contexts.

## Request Timeouts
- Set timeouts on all external HTTP requests to prevent hanging connections.
- Implement retry logic with exponential backoff for transient failures.
- Phase 6 enforces per-tool and per-model async timeouts capped by remaining session time. Retries use capped deterministic exponential backoff and a validated maximum; permanent and unknown failures are not retried.

## Bounded Downloads
- Limit the size of downloaded files (e.g., max 50MB per file).
- Stream large files to disk rather than loading into memory.

## Tool Permission Boundaries
- Tools should operate with least privilege (e.g., read-only access to public web).
- Avoid tools that require elevated system privileges unless absolutely necessary.

## Dependency Hygiene
- Regularly update dependencies to patch known vulnerabilities.
- Use tools like `pip-audit` or `safety` to check for vulnerabilities in dependencies.

## Output Validation
- Validate generated content before presentation (e.g., prevent XSS in web reports).
- Sanitize citations and evidence summaries to prevent injection attacks.

## Logging
- Avoid logging secrets or sensitive user data.
- Log audit trails for security-relevant events (e.g., failed authentication attempts).
- Runtime events contain operation labels, failure categories, attempt counts, delay values, and exception type names only; exception messages, prompts, tool inputs, provider responses, credentials, and evidence content are excluded.
- Deterministic tool failure results expose a stable normalized message and exception type only; raw exception text is excluded from state and API error surfaces.

## Runtime Controls
- `MADRA_EMERGENCY_STOP=true` fails closed before the next iteration or call.
- Every tool/model attempt consumes a bounded request budget; paid external API calls have a separate cap.
- Token counts are enforced where provider usage is measurable, and all calls remain time/request bounded otherwise.
- Runtime failure/report metadata passes through the existing secret-rejecting persistence boundary.

## Model Security
- Treat model outputs as untrusted; validate and sanitize before use in downstream components.
- Be aware of prompt injection risks in agent architectures.

## Phase 7 Security Review
- Mandatory tests cover secret-like API metadata, nested persisted secret fields, credential-bearing URLs, normalized tool failures, and tracked runtime-artifact exclusions.
- The offline demo and mandatory validation need no API keys or network access.
- GitHub workflow permissions are read-only and no repository secrets are requested.
- Dependency vulnerability scanning remains an operator/release-maintenance activity because no frozen Phase 7 dependency-scanner tool or vulnerability database is available offline.
