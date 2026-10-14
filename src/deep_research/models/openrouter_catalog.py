"""Runtime OpenRouter free-model catalog (auto-select, probe, persistent cache).

Queries ``GET /api/v1/models``, keeps only ``:free`` models, ranks text and
vision candidates separately, and refreshes on TTL expiry or after
404 / 429 / unavailable failures. An optional pinned override is tried first;
on failure the next ranked free model is used. No network at import time.

When ``cache_path`` is set the catalog is persisted across restarts:

* first run: discover free models, *probe* the top ``probe_count`` text and
  vision candidates concurrently (tiny prompt, short timeout), and order the
  working ones fastest-first;
* the selection, ranked lists, probe results and a wall-clock timestamp are
  written to a small JSON file (never the API key);
* later processes reuse that file until ``cache_ttl_seconds`` expires, then
  re-discover and re-probe automatically.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any, Literal

import httpx
from pydantic import SecretStr

from deep_research.errors.base import ModelError

AUTO_MODEL_ID = "auto"
#: Synthetic id for FreeModelGuard when the concrete model is chosen at call time.
AUTO_GUARD_MODEL_ID = "auto:free"

CACHE_SCHEMA_VERSION = 1
_PROBE_PROMPT = "Reply with the single word: ok"
#: 1x1 red PNG; proves a model really accepts image input.
_PROBE_IMAGE = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)

_RETRYABLE_STATUS = frozenset({403, 404, 408, 429, 502, 503, 504})
_RETRYABLE_CODES = frozenset(
    {
        "provider_rate_limit",
        "provider_server_error",
        "provider_client_error",
        "provider_connection_error",
        "provider_timeout",
        "provider_quota_exhausted",
        # 200 with an error body / null content (reasoning models out of budget): try the next model.
        "provider_malformed_response",
        "provider_empty_response",
    }
)


@dataclass(frozen=True, slots=True)
class FreeModelInfo:
    """One OpenRouter ``:free`` model after filtering."""

    model_id: str
    context_length: int
    modalities: frozenset[str]
    is_vision: bool


@dataclass(slots=True)
class ProbeResult:
    """Outcome of one tiny availability/latency probe."""

    ok: bool
    latency_ms: float
    status: int | None = None
    error: str | None = None
    kinds: list[str] = field(default_factory=list)


def default_cache_path() -> Path:
    """Per-user cache file (XDG / macOS Caches / Windows LOCALAPPDATA)."""

    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "madra" / "openrouter_free_models.json"


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
        cache_path: Path | str | None = None,
        probe_count: int = 0,
        probe_timeout: float = 8.0,
        clock: Any = time.time,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = http_client
        self._ttl = cache_ttl_seconds
        self._timeout = timeout
        self._cache_path = Path(cache_path).expanduser() if cache_path else None
        self._probe_count = max(0, probe_count)
        self._probe_timeout = probe_timeout
        self._clock = clock
        self._lock = Lock()
        self._loaded_at: float = 0.0
        self._models: list[FreeModelInfo] = []
        self._demoted: set[str] = set()
        self._probes: dict[str, ProbeResult] = {}
        self._selection: dict[str, str] = {}
        self._disk_checked = False

    @property
    def selection(self) -> dict[str, str]:
        """Probed fastest working model per kind (``text`` / ``vision``)."""
        return dict(self._selection)

    @property
    def probes(self) -> dict[str, ProbeResult]:
        return dict(self._probes)

    def invalidate(self) -> None:
        with self._lock:
            self._loaded_at = 0.0
            self._models = []

    def demote(self, model_id: str) -> None:
        """Push a model to the end of future rankings (e.g. after 403/429)."""
        with self._lock:
            self._demoted.add(model_id)

    async def list_models(self, *, force: bool = False) -> list[FreeModelInfo]:
        if not force and not self._disk_checked:
            self._disk_checked = True
            self._load_disk_cache()
        with self._lock:
            fresh = (self._clock() - self._loaded_at) < self._ttl and self._models
            if fresh and not force:
                return list(self._models)
        models = await self._fetch()
        with self._lock:
            self._models = models
            self._loaded_at = self._clock()
        if self._probe_count and not force:
            await self._probe_top(models)
        self._save_disk_cache()
        with self._lock:
            return list(self._models)

    # -- persistence -----------------------------------------------------------

    def _load_disk_cache(self) -> bool:
        """Load a fresh on-disk catalog. Stale, corrupt or foreign files are ignored."""

        if self._cache_path is None:
            return False
        try:
            data: Any = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict) or data.get("version") != CACHE_SCHEMA_VERSION:
            return False
        if data.get("base_url") != self._base_url:
            return False
        saved_at = data.get("saved_at")
        if not isinstance(saved_at, (int, float)) or (self._clock() - float(saved_at)) >= self._ttl:
            return False
        models = [m for m in (_parse_model(row) for row in data.get("models") or []) if m is not None]
        if not models:
            return False
        probes: dict[str, ProbeResult] = {}
        for model_id, row in (data.get("probes") or {}).items():
            if isinstance(model_id, str) and isinstance(row, dict):
                probes[model_id] = ProbeResult(
                    ok=bool(row.get("ok")),
                    latency_ms=float(row.get("latency_ms") or 0.0),
                    status=row.get("status") if isinstance(row.get("status"), int) else None,
                    error=str(row["error"]) if row.get("error") else None,
                    kinds=[str(k) for k in row.get("kinds") or []],
                )
        selection = {str(k): str(v) for k, v in (data.get("selection") or {}).items() if isinstance(v, str)}
        with self._lock:
            self._models = models
            self._loaded_at = float(saved_at)
            self._probes = probes
            self._selection = selection
        return True

    def _save_disk_cache(self) -> None:
        if self._cache_path is None:
            return
        with self._lock:
            models = list(self._models)
            probes = dict(self._probes)
            selection = dict(self._selection)
            saved_at = self._loaded_at
        payload = {
            "version": CACHE_SCHEMA_VERSION,
            "saved_at": saved_at,
            "saved_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(saved_at)),
            "ttl_seconds": self._ttl,
            "base_url": self._base_url,
            "selection": selection,
            "ranked": {kind: self._order(models, kind) for kind in ("text", "vision")},
            "probes": {
                mid: {"ok": p.ok, "latency_ms": round(p.latency_ms, 1), "status": p.status, "error": p.error,
                      "kinds": p.kinds}
                for mid, p in probes.items()
            },
            "models": [
                {"id": m.model_id, "context_length": m.context_length,
                 "architecture": {"input_modalities": sorted(m.modalities)}}
                for m in models
            ],
        }
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(self._cache_path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self._cache_path)
        except OSError:
            return  # cache is an optimisation; never fail a request over it

    # -- probing ---------------------------------------------------------------

    async def _probe_top(self, models: list[FreeModelInfo], *, max_batches: int = 3) -> None:
        """Probe top-ranked text and vision candidates concurrently; fastest working wins.

        Kinds with no working model in a batch get the next ``probe_count``
        candidates probed, up to ``max_batches`` batches.
        """

        probes: dict[str, ProbeResult] = {}
        orders = {kind: self._order(models, kind) for kind in ("text", "vision")}
        for batch in range(max_batches):
            plan: dict[str, list[str]] = {}
            for kind, ordered in orders.items():
                if any(p.ok and kind in p.kinds for p in probes.values()):
                    continue
                window = ordered[batch * self._probe_count:(batch + 1) * self._probe_count]
                for model_id in window:
                    if model_id not in probes:
                        plan.setdefault(model_id, []).append(kind)
            if not plan:
                break
            results = await asyncio.gather(*(self._probe_one(mid, kinds) for mid, kinds in plan.items()))
            probes.update(zip(plan, results))
        with self._lock:
            self._probes = probes
            self._selection = {}
            for kind in ("text", "vision"):
                working = [mid for mid, p in probes.items() if p.ok and kind in p.kinds]
                if working:
                    self._selection[kind] = min(working, key=lambda mid: probes[mid].latency_ms)

    async def _probe_one(self, model_id: str, kinds: list[str]) -> ProbeResult:
        content: Any = _PROBE_PROMPT
        if "vision" in kinds:
            content = [{"type": "text", "text": _PROBE_PROMPT}, {"type": "image_url", "image_url": {"url": _PROBE_IMAGE}}]
        payload = {"model": model_id, "messages": [{"role": "user", "content": content}], "max_tokens": 16}
        client = self._client or httpx.AsyncClient()
        start = time.monotonic()
        try:
            response = await client.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key.get_secret_value()}"},
                json=payload,
                timeout=self._probe_timeout,
            )
        except httpx.HTTPError as exc:
            return ProbeResult(ok=False, latency_ms=_since(start), error=type(exc).__name__, kinds=kinds)
        finally:
            if self._client is None:
                await client.aclose()
        latency = _since(start)
        ok = response.status_code == 200
        error: str | None = None
        if ok:
            try:
                body: Any = response.json()
                ok = isinstance(body, dict) and "error" not in body and bool(body.get("choices"))
                if not ok:
                    error = "error_body"
            except ValueError:
                ok, error = False, "malformed"
        else:
            error = f"http_{response.status_code}"
        return ProbeResult(ok=ok, latency_ms=latency, status=response.status_code, error=error, kinds=kinds)

    async def ranked(
        self,
        kind: Literal["text", "vision"],
        *,
        pin: str | None = None,
        force: bool = False,
    ) -> list[str]:
        """Return ranked free model ids; ``pin`` (if free) is tried first."""

        models = await self.list_models(force=force)
        ids = self._order(models, kind)
        if pin and pin != AUTO_MODEL_ID and pin.endswith(":free"):
            rest = [mid for mid in ids if mid != pin]
            return [pin, *rest]
        return ids

    def _order(self, models: list[FreeModelInfo], kind: Literal["text", "vision"] | str) -> list[str]:
        if kind == "vision":
            candidates = [m for m in models if m.is_vision]
        else:
            # Prefer text-capable models; pure-vision-only models go last.
            textish = [m for m in models if "text" in m.modalities]
            candidates = textish or list(models)
        demoted = set(self._demoted)
        probes = dict(self._probes)
        typed_kind: Literal["text", "vision"] = "vision" if kind == "vision" else "text"
        candidates.sort(key=lambda m: (m.model_id in demoted, _probe_rank(probes.get(m.model_id), typed_kind),
                                       _rank_key(m, kind=typed_kind, demoted=demoted)))
        return [m.model_id for m in candidates]

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


def _probe_rank(probe: ProbeResult | None, kind: Literal["text", "vision"]) -> tuple[int, float]:
    """Probed-working (fastest first) < unprobed < probed-failing."""

    if probe is None or kind not in probe.kinds:
        return (1, 0.0)
    return (0, probe.latency_ms) if probe.ok else (2, 0.0)


def _since(start: float) -> float:
    return (time.monotonic() - start) * 1000


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
