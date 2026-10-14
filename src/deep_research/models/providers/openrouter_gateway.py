"""OpenRouter chat-completions gateway (text generation)."""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import SecretStr

from deep_research.errors.base import ModelError
from deep_research.models.model_gateway import ModelGateway, ModelRequest, ModelResponse


class OpenRouterModelGateway(ModelGateway):
    """Minimal OpenRouter client. No network activity until ``generate``."""

    def __init__(
        self,
        *,
        api_key: SecretStr,
        model: str,
        base_url: str = "https://openrouter.ai/api/v1",
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._client = http_client
        self._timeout = timeout

    def _details(self, **extra: Any) -> dict[str, Any]:
        details: dict[str, Any] = {"provider": "openrouter", "model": self.model}
        details.update(extra)
        return details

    async def generate(self, request: ModelRequest) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": _content(request)}],
        }
        if "max_tokens" in request.parameters:
            payload["max_tokens"] = request.parameters["max_tokens"]
        client = self._client or httpx.AsyncClient()
        try:
            response = await client.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key.get_secret_value()}"},
                json=payload,
                timeout=self._timeout,
            )
        except httpx.TimeoutException:
            raise ModelError("OpenRouter request timed out", error_code="provider_timeout", details=self._details())
        except httpx.HTTPError:
            raise ModelError("OpenRouter network error", error_code="provider_connection_error", details=self._details())
        finally:
            if self._client is None:
                await client.aclose()

        status = response.status_code
        if status >= 400:
            code = {401: "provider_auth_error", 403: "provider_auth_error", 402: "provider_quota_exhausted",
                    429: "provider_rate_limit"}.get(status, "provider_server_error" if status >= 500 else "provider_client_error")
            raise ModelError(f"OpenRouter error (HTTP {status})", error_code=code, details=self._details(status_code=status))
        try:
            data: Any = response.json()
            text = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise ModelError("OpenRouter returned a malformed response", error_code="provider_malformed_response",
                             details=self._details())
        if not isinstance(text, str):
            raise ModelError("OpenRouter content is not a string", error_code="provider_malformed_response",
                             details=self._details())
        usage = data.get("usage") if isinstance(data, dict) else None
        return ModelResponse(text=text, usage=usage if isinstance(usage, dict) else {}, model_id=self.model)

    async def health_check(self) -> bool:
        return bool(self._api_key.get_secret_value().strip())


def _content(request: ModelRequest) -> Any:
    """Plain text, or OpenAI-style multimodal parts when an image is attached."""
    if not request.image_data:
        return request.prompt
    return [
        {"type": "text", "text": request.prompt},
        {"type": "image_url", "image_url": {"url": request.image_data}},
    ]
