"""Runtime OpenRouter free-model catalog (auto-select + cache).

Queries ``GET /api/v1/models``, keeps only ``:free`` models, ranks text and
vision candidates separately, and refreshes on TTL expiry or after
404 / 429 / unavailable failures. An optional pinned override is tried first;
on failure the next ranked free model is used. No network at import time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock
from typing import Any, Literal

import httpx
from pydantic import SecretStr

from deep_research.errors.base import ModelError

AUTO_MODEL_ID = "auto"
#: Synthetic id for FreeModelGuard when the concrete model is chosen at call time.
AUTO_GUARD_MODEL_ID = "auto:free"

_RETRYABLE_STATUS = frozenset({403, 404, 408, 429, 502, 503, 504})
_RETRYABLE_CODES = frozenset(
    {
        "provider_rate_limit",
        "provider_server_error",
        "provider_client_error",
        "provider_connection_error",
        "provider_timeout",
        "provider_quota_exhausted",
    }
)


@dataclass(frozen=True, slots=True)
class FreeModelInfo:
    """One OpenRouter ``:free`` model after filtering."""

    model_id: str
    context_length: int
    modalities: frozenset[str]
    is_vision: bool


class OpenRouterFreeModelCatalog:
    """Cached, ranked list of currently free OpenRouter models.

    Thread-safe. ``list_models`` hits the network only when the cache is empty
    or older than ``cache_ttl_seconds``, or after ``invalidate``.
    """

    def __init__(
        self,
        *,
        api_key: SecretStr,
        base_url: str = "https://openrouter.ai/api/v1",
        http_client: httpx.AsyncClient | None = None,
        cache_ttl_seconds: float = 21_600.0,
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = http_client
        self._ttl = cache_ttl_seconds
        self._timeout = timeout
        self._lock = Lock()
        self._loaded_at: float = 0.0
        self._models: list[FreeModelInfo] = []
        self._demoted: set[str] = set()

    def invalidate(self) -> None:
        with self._lock:
            self._loaded_at = 0.0
            self._models = []

    def demote(self, model_id: str) -> None:
        """Push a model to the end of future rankings (e.g. after 403/429)."""
        with self._lock:
            self._demoted.add(model_id)

    async def list_models(self, *, force: bool = False) -> list[FreeModelInfo]:
        with self._lock:
            fresh = (time.monotonic() - self._loaded_at) < self._ttl and self._models
            if fresh and not force:
                return list(self._models)
        models = await self._fetch()
        with self._lock:
            self._models = models
            self._loaded_at = time.monotonic()
            return list(self._models)

    async def ranked(
        self,
        kind: Literal["text", "vision"],
        *,
        pin: str | None = None,
        force: bool = False,
    ) -> list[str]:
        """Return ranked free model ids; ``pin`` (if free) is tried first."""

        models = await self.list_models(force=force)
        if kind == "vision":
            candidates = [m for m in models if m.is_vision]
        else:
            # Prefer text-capable models; pure-vision-only models go last.
            textish = [m for m in models if "text" in m.modalities]
            candidates = textish or list(models)
        demoted = set(self._demoted)
        candidates.sort(key=lambda m: _rank_key(m, kind=kind, demoted=demoted))
        ids = [m.model_id for m in candidates]
        if pin and pin != AUTO_MODEL_ID and pin.endswith(":free"):
            rest = [mid for mid in ids if mid != pin]
            return [pin, *rest]
        return ids

    async def _fetch(self) -> list[FreeModelInfo]:
        client = self._client or httpx.AsyncClient()
        try:
            response = await client.get(
                f"{self._base_url}/models",
                headers={"Authorization": f"Bearer {self._api_key.get_secret_value()}"},
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise ModelError(
                "OpenRouter models catalog timed out",
                error_code="provider_timeout",
                details={"provider": "openrouter"},
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelError(
                "OpenRouter models catalog network error",
                error_code="provider_connection_error",
                details={"provider": "openrouter"},
            ) from exc
        finally:
            if self._client is None:
                await client.aclose()

        if response.status_code >= 400:
            raise ModelError(
                f"OpenRouter models catalog error (HTTP {response.status_code})",
                error_code="provider_server_error" if response.status_code >= 500 else "provider_client_error",
                details={"provider": "openrouter", "status_code": response.status_code},
            )
        try:
            payload: Any = response.json()
            rows = payload["data"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ModelError(
                "OpenRouter models catalog returned a malformed response",
                error_code="provider_malformed_response",
                details={"provider": "openrouter"},
            ) from exc
        if not isinstance(rows, list):
            raise ModelError(
                "OpenRouter models catalog data is not a list",
                error_code="provider_malformed_response",
                details={"provider": "openrouter"},
            )
        models = [info for info in (_parse_model(row) for row in rows) if info is not None]
        if not models:
            raise ModelError(
                "OpenRouter returned no free (:free) models",
                error_code="no_free_models",
                details={"provider": "openrouter"},
            )
        return models


def _parse_model(row: Any) -> FreeModelInfo | None:
    if not isinstance(row, dict):
        return None
    model_id = row.get("id")
    if not isinstance(model_id, str) or not model_id.endswith(":free"):
        return None
    architecture = row.get("architecture")
    raw_modalities = architecture.get("input_modalities") if isinstance(architecture, dict) else None
    if not isinstance(raw_modalities, list) or not raw_modalities:
        raw_modalities = ["text"]
    modalities = frozenset(str(m).lower() for m in raw_modalities if isinstance(m, str)) or frozenset({"text"})
    try:
        context_length = max(int(row.get("context_length") or 0), 0)
    except (TypeError, ValueError):
        context_length = 0
    return FreeModelInfo(
        model_id=model_id,
        context_length=context_length,
        modalities=modalities,
        is_vision="image" in modalities,
    )


def _rank_key(
    model: FreeModelInfo,
    *,
    kind: Literal["text", "vision"],
    demoted: set[str],
) -> tuple[int, int, int, int, str]:
    """Rank free models for the requested kind.

    * Demoted (recently failed) models last.
    * Specialty (content-safety / note / code) near last.
    * Text requests prefer text-only models (agentic-harness multimodal free
      models often 403 on plain chat); vision requests prefer text+image.
    * Then larger context length, then stable id order.
    """
    specialty = 1 if any(token in model.model_id for token in ("content-safety", "note-preview", "code")) else 0
    if kind == "vision":
        modality_rank = 0 if ("text" in model.modalities and "image" in model.modalities) else 1
    else:
        modality_rank = 0 if model.modalities == frozenset({"text"}) else 1
    return (1 if model.model_id in demoted else 0, specialty, modality_rank, -model.context_length, model.model_id)


def should_fallback(exc: ModelError) -> bool:
    """True when the next free model should be tried."""

    status = exc.details.get("status_code") if isinstance(exc.details, dict) else None
    if isinstance(status, int) and status in _RETRYABLE_STATUS:
        return True
    code = exc.error_code or ""
    if code in _RETRYABLE_CODES:
        return True
    message = (exc.message or "").casefold()
    return any(
        token in message
        for token in (
            "unavailable",
            "not found",
            "rate-limit",
            "rate limit",
            "overloaded",
            "only available",
            "temporarily",
        )
    )
