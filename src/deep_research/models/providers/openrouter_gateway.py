"""OpenRouter chat-completions gateway with automatic free-model fallback."""

from __future__ import annotations

from typing import Any, Literal

import httpx
from pydantic import SecretStr

from deep_research.errors.base import ModelError
from deep_research.models.model_gateway import ModelGateway, ModelRequest, ModelResponse
from deep_research.models.openrouter_catalog import (
    AUTO_GUARD_MODEL_ID,
    AUTO_MODEL_ID,
    OpenRouterFreeModelCatalog,
    should_fallback,
)


class OpenRouterModelGateway(ModelGateway):
    """OpenRouter client that auto-picks a free model and falls back on failure.

    * ``model="auto"`` (default): query ``/models``, rank free models, try in order.
    * ``model="<id>:free"``: try that id first, then the rest of the free catalog.
    * On 404 / 429 / unavailable / overloaded, invalidate the cache and try the next
      free model (up to ``max_attempts``). Vision requests only use image-capable
      free models.
    """

    def __init__(
        self,
        *,
        api_key: SecretStr,
        model: str = AUTO_MODEL_ID,
        base_url: str = "https://openrouter.ai/api/v1",
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 60.0,
        catalog: OpenRouterFreeModelCatalog | None = None,
        catalog_ttl_seconds: float = 21_600.0,
        max_attempts: int = 6,
        kind: Literal["text", "vision"] = "text",
    ) -> None:
        self._api_key = api_key
        cleaned = model.strip()
        self.model = AUTO_MODEL_ID if cleaned in ("", AUTO_MODEL_ID, AUTO_GUARD_MODEL_ID) else cleaned
        self._base_url = base_url.rstrip("/")
        self._client = http_client
        self._timeout = timeout
        self._max_attempts = max(1, max_attempts)
        self._kind: Literal["text", "vision"] = kind
        self._catalog = catalog or OpenRouterFreeModelCatalog(
            api_key=api_key,
            base_url=base_url,
            http_client=http_client,
            cache_ttl_seconds=catalog_ttl_seconds,
            timeout=min(timeout, 30.0),
        )

    def _details(self, **extra: Any) -> dict[str, Any]:
        details: dict[str, Any] = {"provider": "openrouter", "model": self.model}
        details.update(extra)
        return details

    async def generate(self, request: ModelRequest) -> ModelResponse:
        kind: Literal["text", "vision"] = "vision" if request.image_data else self._kind
        pin = None if self.model == AUTO_MODEL_ID else self.model
        last_error: ModelError | None = None
        force_refresh = False
        tried: list[str] = []
        for attempt in range(self._max_attempts):
            candidates = await self._catalog.ranked(kind, pin=pin, force=force_refresh)
            remaining = [mid for mid in candidates if mid not in tried]
            if not remaining:
                break
            model_id = remaining[0]
            tried.append(model_id)
            try:
                return await self._complete(request, model_id)
            except ModelError as exc:
                last_error = exc
                self._catalog.demote(model_id)
                if not should_fallback(exc) or attempt + 1 >= self._max_attempts:
                    raise
                force_refresh = True
                self._catalog.invalidate()
        if last_error is not None:
            raise last_error
        raise ModelError(
            "No free OpenRouter models available to try",
            error_code="no_free_models",
            details=self._details(tried=tried),
        )

    async def _complete(self, request: ModelRequest, model_id: str) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": model_id,
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
        except httpx.TimeoutException as exc:
            raise ModelError(
                "OpenRouter request timed out",
                error_code="provider_timeout",
                details=self._details(model=model_id),
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelError(
                "OpenRouter network error",
                error_code="provider_connection_error",
                details=self._details(model=model_id),
            ) from exc
        finally:
            if self._client is None:
                await client.aclose()

        status = response.status_code
        if status >= 400:
            code = {
                401: "provider_auth_error",
                403: "provider_auth_error",
                402: "provider_quota_exhausted",
                429: "provider_rate_limit",
            }.get(status, "provider_server_error" if status >= 500 else "provider_client_error")
            # Treat "unavailable for free" / missing model as client error too (404).
            body_hint = ""
            try:
                err = response.json().get("error") if response.content else None
                if isinstance(err, dict):
                    body_hint = str(err.get("message") or "")
            except (ValueError, TypeError):
                body_hint = ""
            raise ModelError(
                f"OpenRouter error (HTTP {status})" + (f": {body_hint}" if body_hint else ""),
                error_code=code,
                details=self._details(model=model_id, status_code=status),
            )
        try:
            data: Any = response.json()
            text = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelError(
                "OpenRouter returned a malformed response",
                error_code="provider_malformed_response",
                details=self._details(model=model_id),
            ) from exc
        if not isinstance(text, str):
            raise ModelError(
                "OpenRouter content is not a string",
                error_code="provider_malformed_response",
                details=self._details(model=model_id),
            )
        usage = data.get("usage") if isinstance(data, dict) else None
        return ModelResponse(text=text, usage=usage if isinstance(usage, dict) else {}, model_id=model_id)

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
