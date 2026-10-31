"""Gateway wrapper that enforces ``FreeModelGuard`` before every call."""

from __future__ import annotations

from deep_research.core.free_model_guard import FreeModelGuard, ModelCapability
from deep_research.errors.base import ModelError
from deep_research.models.model_gateway import ModelGateway, ModelRequest, ModelResponse


class GuardedModelGateway(ModelGateway):
    """Delegate to ``inner`` only after the free-model policy check passes."""

    def __init__(
        self,
        inner: ModelGateway,
        *,
        model_id: str,
        provider: str,
        capability: ModelCapability,
        guard: FreeModelGuard,
    ) -> None:
        self._inner = inner
        self.model_id = model_id
        self.provider = provider
        self.capability = capability
        self._guard = guard

    def _check(self, request: ModelRequest, expected: tuple[ModelCapability, ...]) -> None:
        if self.capability not in expected:
            raise ModelError(
                f"Gateway capability '{self.capability.value}' cannot serve this operation",
                error_code="capability_mismatch",
                details={"provider": self.provider, "model": self.model_id},
            )
        requested = request.parameters.get("required_capability")
        self._guard.check(
            provider=self.provider,
            model_id=self.model_id,
            capability=self.capability,
            requested_capability=str(requested) if requested is not None else None,
        )

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self._check(request, (ModelCapability.TEXT_GENERATION, ModelCapability.VISION))
        return await self._inner.generate(request)

    async def transcribe(self, request: ModelRequest) -> ModelResponse:
        self._check(request, (ModelCapability.AUDIO_TRANSCRIPTION,))
        return await self._inner.transcribe(request)

    async def health_check(self) -> bool:
        return await self._inner.health_check()
