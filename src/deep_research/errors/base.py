"""
Base error classes.
"""
from typing import Optional, Dict, Any


class DeepResearchError(Exception):
    """
    Base exception for all deep research application errors.
    """
    def __init__(
        self,
        message: str,
        error_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.message = message
        self.error_code = error_code
        self.details = details or {}
        super().__init__(self.message)


class ConfigurationError(DeepResearchError):
    """
    Raised when there is an error in configuration.
    """
    pass


class ValidationError(DeepResearchError):
    """
    Raised when validation fails.
    """
    pass


class ModelError(DeepResearchError):
    """
    Raised when a model operation fails.
    """
    pass


class ToolError(DeepResearchError):
    """
    Raised when a tool operation fails.
    """
    pass


class ResearchError(DeepResearchError):
    """
    Raised when a research operation fails.
    """
    pass
