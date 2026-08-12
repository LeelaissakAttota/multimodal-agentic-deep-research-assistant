"""
Tool abstraction.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
from pydantic import BaseModel


class ToolRequest(BaseModel):
    """
    Represents a request to a tool.
    """
    tool_name: str
    parameters: Dict[str, Any] = {}


class ToolResult(BaseModel):
    """
    Represents the result of a tool execution.
    """
    success: bool
    output: Any = None
    error: Optional[str] = None
    tool_name: str
    execution_time_ms: Optional[float] = None
    metadata: Dict[str, Any] = {}


class ToolError(Exception):
    """
    Exception raised when a tool fails.
    """
    def __init__(self, tool_name: str, message: str, details: Optional[Dict[str, Any]] = None):
        self.tool_name = tool_name
        self.message = message
        self.details = details or {}
        super().__init__(f"Tool '{tool_name}' failed: {message}")


class Tool(ABC):
    """
    Abstract base class for tools.
    """

    @abstractmethod
    async def execute(self, request: ToolRequest) -> ToolResult:
        """
        Execute the tool with the given request.
        """
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """
        Check if the tool is healthy and ready to use.
        """
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """
        Return the name of the tool.
        """
        pass
