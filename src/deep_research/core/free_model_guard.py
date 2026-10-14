"""Free-only model policy guard.

``FreeModelGuard`` is evaluated before any provider call. It rejects any
(provider, model) pair that is not explicitly free, and any request whose
declared capability does not match the gateway's capability.
"""

from __future__ import annotations

from enum import Enum

from deep_research.errors.base import ModelError


class ModelCapability(str, Enum):
    """Capabilities a model gateway can be authorised for."""

    TEXT_GENERATION = "text_generation"
    VISION = "vision"
    AUDIO_TRANSCRIPTION = "audio_transcription"


#: Synthetic provider ids whose routing is already restricted to a vetted
#: free-only registry (see ``free_stt_router.STT_PROVIDER_REGISTRY``).
_FREE_ROUTER_PROVIDERS: frozenset[str] = frozenset({"free_stt_router"})


class FreeModelGuard:
    """Reject unknown/paid models and capability mismatches before a call."""

    def __init__(self, extra_allowed: frozenset[tuple[str, str]] | None = None) -> None:
        self._extra_allowed = extra_allowed or frozenset()

    def is_free(self, provider: str, model_id: str) -> bool:
        if (provider, model_id) in self._extra_allowed:
            return True
        if provider in _FREE_ROUTER_PROVIDERS:
            return True
        if provider == "openrouter":
            return model_id.endswith(":free") or model_id == "openrouter/free"
        return False

    def check(
        self,
        *,
        provider: str,
        model_id: str,
        capability: ModelCapability,
        requested_capability: str | None = None,
    ) -> None:
        if requested_capability is not None and requested_capability != capability.value:
            raise ModelError(
                f"Requested capability '{requested_capability}' does not match gateway capability '{capability.value}'",
                error_code="capability_mismatch",
                details={"provider": provider, "model": model_id},
            )
        if not self.is_free(provider, model_id):
            raise ModelError(
                f"Model '{provider}/{model_id}' is not an approved free model",
                error_code="paid_model_rejected",
                details={"provider": provider, "model": model_id},
            )
