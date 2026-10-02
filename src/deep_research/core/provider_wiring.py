"""Provider wiring — factory for the real model-provider path (G-12).

This module provides a single factory function ``build_provider_gateway``
that constructs the live model-provider path when credentials are configured,
or returns ``None`` when no credentials are present (preserving offline-safe
behaviour).

Usage pattern::

    from deep_research.core.provider_wiring import build_provider_gateway

    # In application startup:
    provider_gateway = build_provider_gateway(settings)

    if provider_gateway is not None:
        # Live path: policy-guarded → OpenRouter
        routed = RoutedModelGateway(
            routes=[ModelRoute(name="openrouter", gateway=provider_gateway)],
            harness=harness,
        )
    else:
        # Offline/deterministic path: no live provider
        routed = None

Architecture contract
---------------------
* ``build_provider_gateway`` returns ``None`` when ``settings.openrouter_api_key``
  is absent or empty.  No network socket is opened.
* When a key is present, it builds ``OpenRouterModelGateway`` wrapped inside
  ``GuardedModelGateway``.
* ``GuardedModelGateway`` enforces ``FreeModelGuard`` before any provider call.
* Credentials are held in ``SecretStr``; they never appear in repr or logs.
* No implicit network calls occur during import, construction, or startup.
"""

from __future__ import annotations

from deep_research.core.config import Settings
from deep_research.core.free_model_guard import FreeModelGuard, ModelCapability
from deep_research.models.guarded_gateway import GuardedModelGateway
from deep_research.models.model_gateway import ModelGateway
from deep_research.models.free_stt_router import (
    FreeSTTProviderRouter,
    HuggingFaceTranscriptionProvider,
    NVIDIATranscriptionProvider,
    STTProvider,
)
from deep_research.models.providers.openrouter_gateway import OpenRouterModelGateway


def build_provider_gateway(
    settings: Settings,
    *,
    guard: FreeModelGuard | None = None,
    capability: ModelCapability | None = ModelCapability.TEXT_GENERATION,
) -> ModelGateway | None:
    """Build a policy-guarded provider gateway from application settings.

    Dispatch by capability:
    * TEXT_GENERATION / VISION: OpenRouter path (requires OpenRouter key)
    * AUDIO_TRANSCRIPTION: free STT router (requires NVIDIA/HF/OpenRouter key for STT)
    * Other capabilities: None (offline/deterministic)

    Returns ``None`` when no required credentials are configured so that the
    calling code can choose to remain deterministic/offline.

    The returned gateway (when not None) enforces ``FreeModelGuard`` before
    every call — unknown and paid models are rejected before the provider is
    contacted.
    """
    # ----- AUDIO_TRANSCRIPTION: free multi-provider STT router -----
    if capability == ModelCapability.AUDIO_TRANSCRIPTION:
        # Requires at least one of the three STT keys to be present.
        has_any_stt_key = bool(
            settings.nvidia_api_key
            and settings.nvidia_api_key.get_secret_value().strip()
            or settings.huggingface_api_key
            and settings.huggingface_api_key.get_secret_value().strip()
            or settings.openrouter_api_key
            and settings.openrouter_api_key.get_secret_value().strip()
        )
        if not has_any_stt_key:
            return None

        # Build the provider instances from whichever keys are present.
        providers: list[STTProvider] = []
        if settings.nvidia_api_key and settings.nvidia_api_key.get_secret_value().strip():
            providers.append(
                NVIDIATranscriptionProvider(
                    api_key=settings.nvidia_api_key,
                    # Use the default endpoint from the model's catalog page.
                    endpoint="https://2b940e91-a70e-4483-a958-d50408b96589.invocation.api.nvcf.nvidia.com/v1/audio/transcriptions",
                    language="en-US",  # sensible default; can be overridden via settings later if needed
                )
            )
        if settings.huggingface_api_key and settings.huggingface_api_key.get_secret_value().strip():
            providers.append(
                HuggingFaceTranscriptionProvider(
                    api_key=settings.huggingface_api_key,
                    # default base_url and model_id are correct for the serverless endpoint
                )
            )
        if settings.openrouter_api_key and settings.openrouter_api_key.get_secret_value().strip():
            # OpenRouter as a free STT option is ONLY allowed if the exact free model
            # is verified as executable and free via the official STT catalog.
            # As of 2026-10-02, no such model exists, so we DO NOT register it.
            # However, we keep the transport code in place so that if a future
            # verified free STT model appears, enabling it only requires adding the
            # exact (provider_id, model_id) pair to STT_PROVIDER_REGISTRY in
            # free_stt_router.py and restarting.  The router will then pick it up
            # automatically because it reads the registry at import time.
            # For now we skip adding OpenRouter to avoid violating the free-only policy.
            pass

        if not providers:
            # No usable STT provider despite a key being present (should not happen)
            return None

        raw_router = FreeSTTProviderRouter(providers)
        return GuardedModelGateway(
            raw_router,
            model_id="free_stt_router",  # synthetic ID for logging/tracing
            provider="free_stt_router",
            capability=capability,
            guard=guard or FreeModelGuard(),
        )

    # ----- TEXT_GENERATION / VISION: existing OpenRouter-only path -----
    if capability in (ModelCapability.TEXT_GENERATION, ModelCapability.VISION):
        if settings.openrouter_api_key is None:
            return None
        key_value = settings.openrouter_api_key.get_secret_value()
        if not key_value.strip():
            return None

        raw_gateway = OpenRouterModelGateway(
            api_key=settings.openrouter_api_key,
            model=settings.openrouter_model,
            base_url=settings.openrouter_base_url,
        )
        return GuardedModelGateway(
            raw_gateway,
            model_id=settings.openrouter_model,
            provider="openrouter",
            capability=capability,
            guard=guard or FreeModelGuard(),
        )

    # ----- all other capabilities: no live provider -----
    return None
