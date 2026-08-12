"""
Model Gateway abstraction.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
from pydantic import BaseModel


class ModelRequest(BaseModel):
    """
    Represents a request to a language model.
    """
    prompt: str
    parameters: Dict[str, Any] = {}


class ModelResponse(BaseModel):
    """
    Represents a response from a language model.
    """
    text: str
    usage: Dict[str, Any] = {}
    model_id: str
    raw_response: Optional[Any] = None


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
