"""Free-only multi-provider STT router (G-14).

Architecture
------------
    AudioTranscriptionTool
        -> FreeSTTProviderRouter   (implements ModelGateway)
            -> approved provider adapter (NVIDIA / Hugging Face)
                -> provider API

The router is the single execution path for audio transcription.  It only
ever calls providers whose (provider_id, model_id) pair is an exact match in
the static :data:`STT_PROVIDER_REGISTRY` below.  No model ID is ever taken
from a runtime request or an unvalidated environment variable.

Free-only cost policy
---------------------
* ``paid_stt_enabled`` defaults to False and is the only future escape hatch;
  even when set True the registry contains no paid entries, so no paid
  provider can be executed.
* There is no automatic paid fallback anywhere in this module.
* When no approved free provider is usable (missing credentials, quota
  exhaustion, budget exhaustion) the router raises a structured
  ``ModelError(error_code="no_free_stt_provider")`` — it fails closed and
  never substitutes a paid model.

Registry evidence (verified 2026-10-02 against official provider sources)
--------------------------------------------------------------------------
NVIDIA (priority 1)
    Model: ``nvidia/parakeet-tdt-0.6b``
    Endpoint (official catalog API page):
        POST https://2b940e91-a70e-4483-a958-d50408b96589.invocation.api.nvcf.nvidia.com/v1/audio/transcriptions
    Contract: multipart/form-data (``file`` + ``language``), Bearer
    ``NVIDIA_API_KEY``, JSON response ``{"text": "..."}``.
    Free: "All models offer a free trial tier with no credit card required"
    (build.nvidia.com/llms.txt, official).  Finite developer credits
    (1000 at signup, up to 5000); when exhausted requests fail — the API
    catalog has no automatic billing path (no card on file), so usage fails
    closed instead of billing.

Hugging Face (priority 2)
    Model: ``openai/whisper-large-v3`` (serverless "hf-inference" provider)
    Endpoint (official docs):
        POST https://router.huggingface.co/hf-inference/models/openai/whisper-large-v3
    Contract: raw audio bytes (application/octet-stream), Bearer
    ``HUGGINGFACE_API_KEY``, JSON response ``{"text": "..."}``.
    Free: official pricing doc — free users receive $0.10/month of inference
    credits, subject to change; past the credits usage is billed
    pay-as-you-go.  Because the app cannot read the live credit balance, the
    adapter enforces a conservative in-process request budget and fails
    closed when the budget or a 402/429 response is hit.

OpenRouter
    NOT registered: the official STT discovery listing
    (GET /api/v1/models?output_modalities=transcription, 2026-10-02) lists
    24 STT models, all paid; ``openai/whisper-large-v3:free`` is absent and
    has zero live provider endpoints.  The dedicated OpenRouter STT transport
    (OpenRouterModelGateway.transcribe) is preserved but must not be used as
    a free route.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Sequence

import httpx
from pydantic import SecretStr

from deep_research.errors.base import ModelError
from deep_research.models.model_gateway import ModelGateway, ModelRequest, ModelResponse

# ---------------------------------------------------------------------------
# Cost policy + registry
# ---------------------------------------------------------------------------


class STTCostPolicy(str, Enum):
    """Cost classification for a registered STT provider/model entry."""

    FREE_TRIAL_CREDITS = "free_trial_credits"
    """Finite trial credits, no payment method on file; fails closed when
    exhausted (NVIDIA API catalog)."""

    FREE_MONTHLY_CREDITS = "free_monthly_credits"
    """Small monthly free credit allowance; usage beyond it is billed
    pay-as-you-go by the provider.  The adapter must enforce a local budget
    and fail closed (Hugging Face Inference Providers)."""


@dataclass(frozen=True)
class STTProviderEntry:
    """One explicitly approved, evidence-verified free STT provider/model.

    Runtime execution is restricted to entries whose
    ``(provider_id, model_id)`` pair matches exactly.  No dynamic discovery,
    no environment-variable model overrides at execution time.
    """

    provider_id: str
    model_id: str
    priority: int
    cost_policy: STTCostPolicy
    free_verified: bool
    verified_evidence: str
    required_env: str


#: Static, explicit, auditable free STT provider pool.
#:
#: Ordering is evidence-based, not prompt-order:
#: * NVIDIA is first because its free tier has no automatic billing path
#:   (no card on file; exhaustion = request failure).
#: * Hugging Face is second because its $0.10/month allowance continues into
#:   pay-as-you-go billing when a payment method is attached to the account;
#:   the in-process budget is defence-in-depth only.
STT_PROVIDER_REGISTRY: tuple[STTProviderEntry, ...] = (
    STTProviderEntry(
        provider_id="nvidia",
        model_id="nvidia/parakeet-tdt-0.6b",
        priority=1,
        cost_policy=STTCostPolicy.FREE_TRIAL_CREDITS,
        free_verified=True,
        verified_evidence=(
            "build.nvidia.com official catalog page (API section) documents the "
            "endpoint + Bearer auth + multipart contract; build.nvidia.com/llms.txt "
            "(official) states all models offer a free trial tier with no credit "
            "card required; the API catalog has no automatic billing path."
        ),
        required_env="MADRA_NVIDIA_API_KEY",
    ),
    STTProviderEntry(
        provider_id="huggingface",
        model_id="openai/whisper-large-v3",
        priority=2,
        cost_policy=STTCostPolicy.FREE_MONTHLY_CREDITS,
        free_verified=True,
        verified_evidence=(
            "huggingface.co/docs/inference-providers/en/pricing (official): free "
            "users receive $0.10/month of inference credits (subject to change), "
            "billed pay-as-you-go beyond; serverless ASR contract documented at "
            "huggingface.co/tasks/automatic-speech-recognition."
        ),
        required_env="MADRA_HUGGINGFACE_API_KEY",
    ),
    # OpenRouter is intentionally NOT registered: as of 2026-10-02 the
    # official STT catalog (GET /api/v1/models?output_modalities=transcription)
    # lists 24 models, all paid; openai/whisper-large-v3:free has zero live
    # endpoints.  Registering it would violate the free-only policy.
)

#: The exact (provider_id, model_id) pairs executable at runtime.
_APPROVED_STT_PAIRS: frozenset[tuple[str, str]] = frozenset((entry.provider_id, entry.model_id) for entry in STT_PROVIDER_REGISTRY)


def approved_stt_pairs() -> frozenset[tuple[str, str]]:
    """Return the exact approved (provider_id, model_id) pairs."""
    return _APPROVED_STT_PAIRS


# ---------------------------------------------------------------------------
# Provider abstraction
# ---------------------------------------------------------------------------


class STTProvider:
    """Base class for approved free STT provider adapters.

    Subclasses implement :meth:`is_available` and :meth:`transcribe`.
    Adapters raise :class:`ModelError` with a structured ``error_code`` for
    every failure class and never include credentials in messages/details.
    """

    provider_id: str = "abstract"
    model_id: str = "abstract"

    def __init__(self, model_id: str | None = None) -> None:
        # A model override is only accepted when it matches an approved
        # registry entry; the check below enforces that either way.
        if model_id is not None:
            self.model_id = model_id
        # Enforce registry membership at construction: an adapter whose
        # (provider, model) is not an approved entry cannot be built.
        if (self.provider_id, self.model_id) not in _APPROVED_STT_PAIRS:
            raise ModelError(
                (f"STT provider '{self.provider_id}/{self.model_id}' is not in the approved free STT registry"),
                error_code="stt_provider_not_approved",
                details={"provider": self.provider_id, "model": self.model_id},
            )

    def is_available(self) -> bool:
        """Return True when the provider can be used right now.

        Default: always available.  Subclasses override to check
        credentials, budgets, and enabled flags.  This call must be
        side-effect free and must never open a network socket.
        """
        return True

    async def transcribe(self, audio_bytes: bytes, audio_format: str) -> ModelResponse:
        """Transcribe *audio_bytes* and return the provider's text result."""
        raise NotImplementedError

    # -- shared helpers -----------------------------------------------------

    @staticmethod
    def _safe_details(provider: str, model: str, **extra: Any) -> dict[str, Any]:
        """Build error details that can never carry a credential."""
        details: dict[str, Any] = {"provider": provider, "model": model}
        details.update(extra)
        return details

    @staticmethod
    def _parse_stt_json(response: httpx.Response, provider: str, model: str) -> ModelResponse:
        """Parse a ``{"text": "..."}`` STT response with a shared error map.

        HTTP error mapping (identical shape to OpenRouterModelGateway):
        401/403 -> provider_auth_error, 402 -> provider_quota_exhausted,
        429 -> provider_rate_limit, 5xx -> provider_server_error,
        other 4xx -> provider_client_error.
        """
        status = response.status_code
        if status in (401, 403):
            raise ModelError(
                f"{provider} STT authentication failed (HTTP {status})",
                error_code="provider_auth_error",
                details=STTProvider._safe_details(provider, model, status_code=status),
            )
        if status == 402:
            raise ModelError(
                f"{provider} STT free quota exhausted (HTTP 402); failing closed, no paid usage",
                error_code="provider_quota_exhausted",
                details=STTProvider._safe_details(provider, model, status_code=status),
            )
        if status == 429:
            raise ModelError(
                f"{provider} STT rate limit exceeded (HTTP 429)",
                error_code="provider_rate_limit",
                details=STTProvider._safe_details(provider, model, status_code=status),
            )
        if 500 <= status < 600:
            raise ModelError(
                f"{provider} STT server error (HTTP {status})",
                error_code="provider_server_error",
                details=STTProvider._safe_details(provider, model, status_code=status),
            )
        if status >= 400:
            raise ModelError(
                f"{provider} STT client error (HTTP {status})",
                error_code="provider_client_error",
                details=STTProvider._safe_details(provider, model, status_code=status),
            )

        try:
            data: Any = response.json()
        except (json.JSONDecodeError, ValueError):
            raise ModelError(
                f"{provider} STT returned a malformed JSON response",
                error_code="provider_malformed_response",
                details=STTProvider._safe_details(provider, model),
            )
        if not isinstance(data, dict) or "text" not in data:
            raise ModelError(
                f"{provider} STT response missing 'text' field",
                error_code="provider_malformed_response",
                details=STTProvider._safe_details(provider, model),
            )
        text = data["text"]
        if not isinstance(text, str):
            raise ModelError(
                f"{provider} STT 'text' field is not a string",
                error_code="provider_malformed_response",
                details=STTProvider._safe_details(provider, model),
            )
        if not text.strip():
            raise ModelError(
                f"{provider} STT response contained empty transcription",
                error_code="provider_empty_response",
                details=STTProvider._safe_details(provider, model),
            )
        return ModelResponse(text=text, usage={}, model_id=model)


class NVIDIATranscriptionProvider(STTProvider):
    """NVIDIA API catalog ASR NIM adapter (free developer trial tier).

    Contract (official catalog API page, verified 2026-10-02):

    * ``POST <endpoint>`` where the default endpoint is the documented
      hosted URL for ``nvidia/parakeet-tdt-0.6b``:
      ``https://2b940e91-a70e-4483-a958-d50408b96589.invocation.api.nvcf.nvidia.com/v1/audio/transcriptions``
    * ``Authorization: Bearer <NVIDIA_API_KEY>``
    * multipart/form-data body: ``file`` (audio bytes + MIME type),
      ``language`` (BCP-47 code), optional ``response_format=json``
    * JSON response: ``{"text": "..."}``

    Cost safety: the API catalog has no automatic billing path — when trial
    credits are exhausted the request fails (402/429 or provider error) and
    this provider is skipped; nothing is billed silently.
    """

    provider_id = "nvidia"
    model_id = "nvidia/parakeet-tdt-0.6b"

    def __init__(
        self,
        *,
        api_key: SecretStr | None,
        endpoint: str,
        language: str = "en-US",
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 60.0,
    ) -> None:
        super().__init__()
        self._api_key = api_key
        self._endpoint = endpoint
        self._language = language
        self._timeout = timeout
        self._external_client = http_client
        self._owned_client: httpx.AsyncClient | None = None

    # -- availability --------------------------------------------------------

    def is_available(self) -> bool:
        if self._api_key is None:
            return False
        return bool(self._api_key.get_secret_value().strip())

    def _get_key_or_raise(self) -> str:
        key = self._api_key.get_secret_value() if self._api_key else ""
        if not key.strip():
            raise ModelError(
                "NVIDIA API key is not configured (set MADRA_NVIDIA_API_KEY)",
                error_code="provider_auth_error",
                details=STTProvider._safe_details(self.provider_id, self.model_id),
            )
        return key

    def _get_client(self) -> httpx.AsyncClient:
        if self._external_client is not None:
            return self._external_client
        if self._owned_client is None:
            self._owned_client = httpx.AsyncClient()
        return self._owned_client

    # -- transcription --------------------------------------------------------

    async def transcribe(self, audio_bytes: bytes, audio_format: str) -> ModelResponse:
        key = self._get_key_or_raise()
        client = self._get_client()
        mime = _format_to_mime(audio_format)
        filename = f"audio.{audio_format}"

        try:
            response = await client.post(
                self._endpoint,
                headers={"Authorization": f"Bearer {key}"},
                files={
                    "file": (filename, audio_bytes, mime),
                    "language": (None, self._language),
                    "response_format": (None, "json"),
                },
                timeout=self._timeout,
            )
        except httpx.TimeoutException:
            raise ModelError(
                "NVIDIA STT request timed out",
                error_code="provider_timeout",
                details=STTProvider._safe_details(self.provider_id, self.model_id),
            )
        except httpx.ConnectError:
            raise ModelError(
                "Could not connect to NVIDIA STT endpoint",
                error_code="provider_connection_error",
                details=STTProvider._safe_details(self.provider_id, self.model_id),
            )
        except httpx.NetworkError:
            raise ModelError(
                "Network error while calling NVIDIA STT endpoint",
                error_code="provider_connection_error",
                details=STTProvider._safe_details(self.provider_id, self.model_id),
            )

        return self._parse_stt_json(response, self.provider_id, self.model_id)

    async def aclose(self) -> None:
        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None


class HuggingFaceTranscriptionProvider(STTProvider):
    """Hugging Face serverless ("hf-inference") ASR adapter.

    Contract (official docs, verified 2026-10-02):

    * ``POST https://router.huggingface.co/hf-inference/models/<model_id>``
    * ``Authorization: Bearer <HUGGINGFACE_API_KEY>``
    * Raw audio bytes body, ``Content-Type: application/octet-stream``
    * JSON response: ``{"text": "..."}``

    Cost safety: HF free users get a small monthly credit allowance
    ($0.10, subject to change); beyond it, usage is billed pay-as-you-go if
    a payment method is attached.  This adapter therefore:

    * enforces a conservative in-process request budget
      (``max_requests``); once the budget is used the provider reports
      itself unavailable and the router fails closed;
    * treats HTTP 402 as quota exhaustion and immediately disables the
      in-process budget so no further requests are attempted;
    * treats HTTP 429 as rate-limit exhaustion (retryable later, but not by
      burning more free credits now — the router moves on or fails closed).
    """

    provider_id = "huggingface"
    model_id = "openai/whisper-large-v3"

    def __init__(
        self,
        *,
        api_key: SecretStr | None,
        base_url: str = "https://router.huggingface.co/hf-inference/models/",
        model_id: str = "openai/whisper-large-v3",
        max_requests: int = 20,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 60.0,
    ) -> None:
        # Model override is still registry-validated by the base class.
        super().__init__(model_id=model_id)
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._max_requests = max(0, int(max_requests))
        self._requests_used = 0
        self._budget_disabled = False
        self._timeout = timeout
        self._external_client = http_client
        self._owned_client: httpx.AsyncClient | None = None

    # -- availability --------------------------------------------------------

    def is_available(self) -> bool:
        if self._budget_disabled:
            return False
        if self._requests_used >= self._max_requests:
            return False
        if self._api_key is None:
            return False
        return bool(self._api_key.get_secret_value().strip())

    def _get_key_or_raise(self) -> str:
        key = self._api_key.get_secret_value() if self._api_key else ""
        if not key.strip():
            raise ModelError(
                "Hugging Face API key is not configured (set MADRA_HUGGINGFACE_API_KEY)",
                error_code="provider_auth_error",
                details=STTProvider._safe_details(self.provider_id, self.model_id),
            )
        return key

    def _get_client(self) -> httpx.AsyncClient:
        if self._external_client is not None:
            return self._external_client
        if self._owned_client is None:
            self._owned_client = httpx.AsyncClient()
        return self._owned_client

    # -- transcription --------------------------------------------------------

    async def transcribe(self, audio_bytes: bytes, audio_format: str) -> ModelResponse:
        if not self.is_available():
            raise ModelError(
                "Hugging Face STT free budget exhausted; failing closed, no paid usage",
                error_code="provider_quota_exhausted",
                details=STTProvider._safe_details(self.provider_id, self.model_id, requests_used=self._requests_used),
            )
        key = self._get_key_or_raise()
        client = self._get_client()

        try:
            response = await client.post(
                f"{self._base_url}/{self.model_id}",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/octet-stream",
                },
                content=audio_bytes,
                timeout=self._timeout,
            )
        except httpx.TimeoutException:
            raise ModelError(
                "Hugging Face STT request timed out",
                error_code="provider_timeout",
                details=STTProvider._safe_details(self.provider_id, self.model_id),
            )
        except httpx.ConnectError:
            raise ModelError(
                "Could not connect to Hugging Face STT endpoint",
                error_code="provider_connection_error",
                details=STTProvider._safe_details(self.provider_id, self.model_id),
            )
        except httpx.NetworkError:
            raise ModelError(
                "Network error while calling Hugging Face STT endpoint",
                error_code="provider_connection_error",
                details=STTProvider._safe_details(self.provider_id, self.model_id),
            )

        if response.status_code == 402:
            # Server-side free quota exhausted: disable the local budget so
            # no further request is even attempted (fail closed).
            self._budget_disabled = True

        result = self._parse_stt_json(response, self.provider_id, self.model_id)
        self._requests_used += 1
        return result

    async def aclose(self) -> None:
        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None


# ---------------------------------------------------------------------------
# FreeSTTProviderRouter — the ModelGateway execution path for G-14
# ---------------------------------------------------------------------------


class FreeSTTProviderRouter(ModelGateway):
    """Route transcription requests to the first usable approved free provider.

    Implements :class:`ModelGateway` so existing callers
    (``AudioTranscriptionTool``) keep their ``gateway.transcribe(request)``
    contract.

    Routing rules (deterministic, fail-closed):

    1. Providers are considered in registry priority order.
    2. Providers whose :meth:`STTProvider.is_available` is False are skipped
       (missing credentials, budget/quota exhausted) — no request is sent.
    3. Provider errors (auth, quota, rate limit, server, timeout,
       connection, malformed) move the router to the next approved free
       provider.  No error ever escalates to a paid provider: no paid
       provider exists in the registry.
    4. If every provider is unavailable or fails, a structured
       ``ModelError(error_code="no_free_stt_provider")`` is raised with the
       per-provider attempt record in ``details``.
    5. :meth:`generate` is rejected: this router is an STT-only gateway and
       must never become a backdoor to paid text/vision models.
    """

    def __init__(
        self,
        providers: Sequence[STTProvider],
        *,
        paid_stt_enabled: bool = False,
    ) -> None:
        if not providers:
            raise ModelError(
                "No STT providers configured; audio transcription fails closed",
                error_code="no_free_stt_provider",
                details={"attempts": []},
            )
        # Registry validation: every provider must be an approved entry.
        for provider in providers:
            if (provider.provider_id, provider.model_id) not in _APPROVED_STT_PAIRS:
                raise ModelError(
                    (f"STT provider '{provider.provider_id}/{provider.model_id}' is not in the approved free STT registry"),
                    error_code="stt_provider_not_approved",
                    details={
                        "provider": provider.provider_id,
                        "model": provider.model_id,
                    },
                )
        if paid_stt_enabled:
            # Documented no-op today: the registry contains only free
            # entries, so enabling this flag changes nothing.  It exists so
            # a future paid extension cannot be smuggled in silently.
            if any(
                entry.cost_policy
                not in {
                    STTCostPolicy.FREE_TRIAL_CREDITS,
                    STTCostPolicy.FREE_MONTHLY_CREDITS,
                }
                for entry in STT_PROVIDER_REGISTRY
            ):
                raise ModelError(
                    "Paid STT entries are not permitted under the free-only policy",
                    error_code="paid_stt_policy_violation",
                    details={},
                )
        self._providers = tuple(sorted(providers, key=_provider_priority))

    # ------------------------------------------------------------------
    # ModelGateway interface
    # ------------------------------------------------------------------

    async def transcribe(self, request: ModelRequest) -> ModelResponse:
        audio_data = request.audio_data or ""
        if not audio_data.strip():
            raise ModelError(
                "STT request has no audio payload (audio_data is empty)",
                error_code="invalid_stt_request",
                details={"provider": "router"},
            )

        audio_bytes, audio_format = _decode_audio_data_url(audio_data)

        attempts: list[dict[str, Any]] = []
        last_error: ModelError | None = None
        for provider in self._providers:
            provider_label = f"{provider.provider_id}/{provider.model_id}"
            if not provider.is_available():
                attempts.append({"provider": provider.provider_id, "model": provider.model_id, "outcome": "skipped_unavailable"})
                continue
            try:
                return await provider.transcribe(audio_bytes, audio_format)
            except ModelError as exc:
                last_error = exc
                attempts.append(
                    {
                        "provider": provider.provider_id,
                        "model": provider.model_id,
                        "outcome": "failed",
                        "error_code": exc.error_code,
                    }
                )
                continue
            except Exception:
                last_error = ModelError(
                    f"{provider_label} raised an unexpected error",
                    error_code="provider_unexpected_error",
                    details=STTProvider._safe_details(provider.provider_id, provider.model_id),
                )
                attempts.append(
                    {
                        "provider": provider.provider_id,
                        "model": provider.model_id,
                        "outcome": "failed",
                        "error_code": "provider_unexpected_error",
                    }
                )
                continue

        # All approved free providers unavailable or failed → fail closed.
        raise ModelError(
            "No free STT provider is currently available; failing closed. No paid provider will be used.",
            error_code="no_free_stt_provider",
            details={"attempts": attempts, "last_error_code": getattr(last_error, "error_code", None)},
        )

    async def generate(self, request: ModelRequest) -> ModelResponse:
        # STT-only gateway: text/vision generation must never be routed
        # through the free STT path (prevents accidental paid-model use).
        raise ModelError(
            "FreeSTTProviderRouter supports transcription only; text generation is not routed through this gateway",
            error_code="stt_router_generate_not_supported",
            details={"provider": "router"},
        )

    async def health_check(self) -> bool:
        """Healthy when at least one approved free provider is usable."""
        return any(provider.is_available() for provider in self._providers)

    @property
    def providers(self) -> tuple[STTProvider, ...]:
        """The approved providers in routing (priority) order."""
        return self._providers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: Registry priority lookup for routing order.
_PRIORITY_BY_PROVIDER: dict[str, int] = {entry.provider_id: entry.priority for entry in STT_PROVIDER_REGISTRY}


def _provider_priority(provider: STTProvider) -> int:
    return _PRIORITY_BY_PROVIDER.get(provider.provider_id, 10**9)


def _decode_audio_data_url(data_url: str) -> tuple[bytes, str]:
    """Decode a ``data:audio/<mime>;base64,<data>`` URL into (bytes, format).

    Raises ``ModelError(invalid_stt_request)`` for anything that is not a
    recognised audio data URL — the router never guesses encodings.
    """
    lowered = data_url.strip().lower()
    if not lowered.startswith("data:") or ";base64," not in lowered:
        raise ModelError(
            "STT audio payload must be a data URL of the form 'data:audio/<mime>;base64,<data>'",
            error_code="invalid_stt_request",
            details={"provider": "router"},
        )
    header = lowered.split(";base64,", 1)[0]
    try:
        mime = header.split(":", 1)[1]  # 'audio/wav'
        fmt = mime.split("/", 1)[1]
    except (IndexError, ValueError):
        raise ModelError(
            "STT audio data URL has an unparseable MIME header",
            error_code="invalid_stt_request",
            details={"provider": "router"},
        )
    fmt = _FORMAT_ALIASES.get(fmt, fmt)
    payload = data_url.strip()[len(header) + len(";base64,") :]
    try:
        import base64

        audio_bytes = base64.b64decode(payload, validate=True)
    except Exception:
        raise ModelError(
            "STT audio payload is not valid base64",
            error_code="invalid_stt_request",
            details={"provider": "router"},
        )
    return audio_bytes, fmt


#: MIME sub-type normalisation (matches the OpenRouter gateway aliases).
_FORMAT_ALIASES: dict[str, str] = {"mpeg": "mp3", "x-m4a": "m4a"}


def _format_to_mime(fmt: str) -> str:
    """Map a short audio format to its MIME type for multipart uploads."""
    table = {
        "wav": "audio/wav",
        "mp3": "audio/mpeg",
        "ogg": "audio/ogg",
        "webm": "audio/webm",
        "flac": "audio/flac",
        "m4a": "audio/mp4",
        "mp4": "audio/mp4",
    }
    return table.get(fmt, "application/octet-stream")
