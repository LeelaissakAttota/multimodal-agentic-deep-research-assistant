# Model Policy

## Development Model
- During development, the system may use the Hermes agent's built-in model (Nemotron family) for convenience.
- This model is **not** part of the product architecture and is only used for agentic development assistance.

## Runtime Model Abstraction
- The product architecture includes a **model gateway** that abstracts interactions with LLM providers.
- This gateway allows the research engine to work with any compatible LLM provider (OpenAI, Anthropic, local models, etc.).
- No component of the research engine is directly coupled to a specific model provider.

## Configuration
- Model provider selection, API keys, and model names are configured via environment variables.
- Example variables:
  - `MADRA_MODEL_PROVIDER` (e.g., `openai`, `anthropic`, `local`)
  - `MADRA_MODEL_NAME` (e.g., `gpt-4o`, `claude-3-5-sonnet-20240620`)
  - `MADRA_API_KEY` (provider-specific, loaded from environment)

## Fallback and Routing
- The model gateway may implement fallback strategies (e.g., try primary provider, fall back to secondary on failure).
- Fallback behavior is configurable.

## Token Limits and Budgets
- The system will support configurable token budgets per research operation (to be implemented in later phases).
- Model calls will be tracked and can be limited to control costs.

## Safety and Bias
- While the model gateway does not enforce safety filters, it is expected that providers implement their own safety measures.
- The research engine includes evaluation steps to check for bias, hallucination, and unsupported claims in model outputs.

## Prohibited Practices
- **No hardcoded API keys** in source code or configuration files.
- **No direct imports** of provider-specific SDKs in core research logic (use the gateway).
- **No assumption** of a specific model's capabilities beyond what is defined in the gateway interface.
