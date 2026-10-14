"""
Model Gateway abstraction.
"""
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field

from deep_research.errors.base import ModelError


class ModelRequest(BaseModel):
    """
    Represents a request to a language model.
    """
    prompt: str
    audio_data: str | None = Field(
        default=None,
        description="Optional audio payload as a 'data:audio/<mime>;base64,<data>' URL (transcription only).",
    )
    parameters: dict[str, Any] = Field(default_factory=dict)


class ModelResponse(BaseModel):
    """
    Represents a response from a language model.
    """
    text: str
    usage: dict[str, Any] = Field(default_factory=dict)
    model_id: str
    raw_response: Any | None = None


class ModelGateway(ABC):
    """
    Abstract base class for model gateways.
    """

    @abstractmethod
    async def generate(self, request: ModelRequest) -> ModelResponse:
        """
        Generate a response from the model.
        """
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """
        Check if the model service is healthy.
        """
        pass

    async def transcribe(self, request: ModelRequest) -> ModelResponse:
        """
        Transcribe ``request.audio_data`` to text.

        Optional capability: the default implementation fails closed with a
        structured ``ModelError`` so text-only gateways never pretend to
        support audio. Transcription-capable gateways override this.
        """
        raise ModelError(
            f"{type(self).__name__} does not support audio transcription",
            error_code="transcription_not_supported",
            details={"gateway": type(self).__name__},
        )
