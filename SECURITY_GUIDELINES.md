# Security Guidelines

## Secrets Management
- All secrets (API keys, tokens, etc.) must be stored in environment variables.
- Never hardcode secrets in source code.
- Use `.env` file for local development (excluded from Git via .gitignore).
- In production, use secure secret management (e.g., HashiCorp Vault, AWS Secrets Manager).

## Environment Variables
- Prefix environment variables with `MADRA_` (Multimodal Agentic Deep Research Assistant) to avoid collisions.
- Example: `MADRA_OPENAI_API_KEY`, `MADRA_GOOGLE_SEARCH_API_KEY`.

## Input Validation
- Validate all external inputs (user queries, API responses, file uploads).
- Use allowlists where possible (e.g., allowed URL schemes, file types).
- Reject or sanitize inputs that do not conform to expectations.

## Safe File Handling
- Restrict file operations to designated directories (e.g., `data/`, `reports/`).
- Validate file paths to prevent directory traversal attacks.
- Impose size limits on uploads and downloads.

## URL Validation
- Validate URLs for allowed schemes (http, https) and block dangerous schemes (file, gopher, etc.).
- Consider using allowlists for trusted domains in research contexts.

## Request Timeouts
- Set timeouts on all external HTTP requests to prevent hanging connections.
- Implement retry logic with exponential backoff for transient failures.

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

## Model Security
- Treat model outputs as untrusted; validate and sanitize before use in downstream components.
- Be aware of prompt injection risks in agent architectures.
