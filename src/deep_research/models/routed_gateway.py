"""Provider-neutral bounded model routing and fallback."""

from __future__ import annotations

from dataclasses import dataclass

from deep_research.errors.base import ModelError
from deep_research.models.model_gateway import ModelGateway, ModelRequest, ModelResponse
from deep_research.runtime.contracts import FailureKind, RuntimeControlError
from deep_research.runtime.harness import ExecutionHarness


@dataclass(frozen=True)
class ModelRoute:
    """Named model provider route without provider-specific imports."""

    name: str
    gateway: ModelGateway


class RoutedModelGateway(ModelGateway):
    """Try ordered provider routes after bounded transient failures only."""

    def __init__(
        self,
        routes: list[ModelRoute],
        harness: ExecutionHarness,
    ) -> None:
        if not routes:
            raise ValueError("At least one model route is required")
        names = [route.name for route in routes]
        if any(not name.strip() for name in names):
            raise ValueError("Model route names cannot be blank")
        if len(names) != len(set(names)):
            raise ValueError("Model route names must be unique")
        self.routes = list(routes)
        self.harness = harness

    async def generate(self, request: ModelRequest) -> ModelResponse:
        requested_tokens = request.parameters.get("max_tokens", 0)
        if not isinstance(requested_tokens, int) or requested_tokens < 0:
            raise ModelError(
                "Model max_tokens must be a non-negative integer",
                error_code="invalid_model_request",
            )

        failures: list[dict[str, str | int | None]] = []
        for index, route in enumerate(self.routes):
            async def provider_call() -> ModelResponse:
                return await route.gateway.generate(request)

            try:
                return await self.harness.execute_model(
                    f"model:{route.name}",
                    provider_call,
                    requested_tokens=requested_tokens,
                )
            except RuntimeControlError as exc:
                failures.append(
                    {
                        "route": route.name,
                        "failure_kind": exc.failure_kind.value,
                        "attempts": exc.attempts,
                    }
                )
                can_fallback = exc.failure_kind in {
                    FailureKind.TRANSIENT,
                    FailureKind.TIMEOUT,
                }
                if can_fallback and index + 1 < len(self.routes):
                    self.harness.emit(
                        "model_fallback",
                        operation=f"model:{route.name}",
                        failure_kind=exc.failure_kind,
                        metadata={"next_route": self.routes[index + 1].name},
                    )
                    continue
                raise ModelError(
                    "All eligible model routes failed",
                    error_code="model_routes_exhausted",
                    details={"routes": failures},
                ) from exc
        raise AssertionError("Model routing exited without a response or failure")

    async def health_check(self) -> bool:
        """Ready when at least one route reports healthy."""
        for route in self.routes:
            try:
                async def provider_health_check() -> bool:
                    return await route.gateway.health_check()

                if await self.harness.execute_model(
                    f"model_health:{route.name}", provider_health_check
                ):
                    return True
            except RuntimeControlError:
                continue
        return False
