"""
Model Gateway abstraction.
"""
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class ModelRequest(BaseModel):
    """
    Represents a request to a language model.
    """
    prompt: str
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
