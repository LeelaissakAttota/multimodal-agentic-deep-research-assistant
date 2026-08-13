"""
Tool definitions and contracts for multimodal research tools.
"""
from enum import Enum
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field
from deep_research.domain.modality import Modality


class ToolCapability(str, Enum):
    """Capabilities that a tool can provide."""
    SEARCH = "search"
    READ = "read"
    ANALYZE = "analyze"
    TRANSCRIBE = "transcribe"
    DESCRIBE = "describe"
    EXTRACT = "extract"


class ToolInput(BaseModel):
    """Base model for tool input parameters."""
    pass


class ToolOutput(BaseModel):
    """Base model for tool output."""
    pass


class ToolDefinition(BaseModel):
    """Definition of a tool's capabilities and metadata."""
    identifier: str = Field(..., description="Unique identifier for the tool")
    name: str = Field(..., description="Human-readable name of the tool")
    description: str = Field(..., description="Description of what the tool does")
    modality: Modality = Field(..., description="Primary modality the tool operates on")
    capabilities: List[ToolCapability] = Field(
        default_factory=list, description="List of capabilities the tool provides"
    )
    input_schema: type[ToolInput] = Field(
        ..., description="Pydantic model for validating tool input"
    )
    output_schema: type[ToolOutput] = Field(
        ..., description="Pydantic model for validating tool output"
    )
    cost_class: str = Field(
        default="FREE", description="Cost class: FREE, METERED, PAID, etc."
    )
    network_required: bool = Field(
        default=False, description="Whether the tool requires network access"
    )
    auth_required: bool = Field(
        default=False, description="Whether the tool requires authentication"
    )
    reliability: str = Field(
        default="MEDIUM", description="Reliability indicator: LOW, MEDIUM, HIGH"
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict, description="Additional tool-specific metadata"
    )


class ToolExecutionResult(BaseModel):
    """Result of executing a tool."""
    tool_identifier: str = Field(..., description="Identifier of the tool that was executed")
    success: bool = Field(..., description="Whether the tool execution succeeded")
    output: Optional[ToolOutput] = Field(
        None, description="Normalized output from the tool"
    )
    error: Optional[str] = Field(
        None, description="Error message if execution failed"
    )
    execution_time_ms: float = Field(..., description="Execution time in milliseconds")
    metadata: Dict[str, Any] = Field(
        default_factory=dict, description="Additional execution metadata"
    )


class ToolError(Exception):
    """Exception raised when a tool fails."""
    def __init__(
        self,
        tool_identifier: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.tool_identifier = tool_identifier
        self.message = message
        self.details = details or {}
        super().__init__(
            f"Tool '{tool_identifier}' failed: {message}"
        )


class ToolNotFoundError(ToolError):
    """Raised when a tool identifier is not found in the registry."""
    def __init__(self, tool_identifier: str):
        super().__init__(
            tool_identifier,
            f"Tool not found: {tool_identifier}",
            {"tool_identifier": tool_identifier},
        )


class ToolValidationError(ToolError):
    """Raised when tool input validation fails."""
    def __init__(
        self,
        tool_identifier: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            tool_identifier,
            f"Tool validation error: {message}",
            details or {"tool_identifier": tool_identifier},
        )


class ToolExecutionError(ToolError):
    """Raised when tool execution fails."""
    def __init__(
        self,
        tool_identifier: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            tool_identifier,
            f"Tool execution error: {message}",
            details or {"tool_identifier": tool_identifier},
        )


class ToolTimeoutError(ToolError):
    """Raised when tool execution times out."""
    def __init__(
        self,
        tool_identifier: str,
        message: str = "Tool execution timed out",
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            tool_identifier,
            message,
            details or {"tool_identifier": tool_identifier},
        )
