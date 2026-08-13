"""
Tests for the tool definition module.
"""
import pytest
from deep_research.tools.definition import (
    ToolDefinition, ToolCapability, ToolInput, ToolOutput,
    ToolExecutionResult, ToolError, ToolNotFoundError,
    ToolValidationError, ToolExecutionError, ToolTimeoutError
)
from deep_research.domain.modality import Modality


class TestToolInput(ToolInput):
    query: str


class TestToolOutput(ToolOutput):
    result: str


def test_tool_definition_creation():
    """Test creating a tool definition."""
    definition = ToolDefinition(
        identifier="test_tool",
        name="Test Tool",
        description="A test tool",
        modality=Modality.TEXT,
        capabilities=[ToolCapability.SEARCH],
        input_schema=TestToolInput,
        output_schema=TestToolOutput
    )

    assert definition.identifier == "test_tool"
    assert definition.name == "Test Tool"
    assert definition.description == "A test tool"
    assert definition.modality == Modality.TEXT
    assert definition.capabilities == [ToolCapability.SEARCH]
    assert definition.input_schema == TestToolInput
    assert definition.output_schema == TestToolOutput


def test_tool_execution_result_creation():
    """Test creating a tool execution result."""
    result = ToolExecutionResult(
        tool_identifier="test_tool",
        success=True,
        output=TestToolOutput(result="test"),
        execution_time_ms=100.0
    )

    assert result.tool_identifier == "test_tool"
    assert result.success is True
    assert result.output.result == "test"
    assert result.execution_time_ms == 100.0


def test_tool_error_creation():
    """Test creating tool errors."""
    # Base ToolError
    error = ToolError("test_tool", "Something went wrong")
    assert error.tool_identifier == "test_tool"
    assert error.message == "Something went wrong"
    assert "Tool 'test_tool' failed: Something went wrong" in str(error)

    # ToolNotFoundError
    not_found = ToolNotFoundError("missing_tool")
    assert not_found.tool_identifier == "missing_tool"
    assert "Tool not found: missing_tool" in str(not_found)

    # ToolValidationError
    validation_error = ToolValidationError("test_tool", "Invalid input")
    assert validation_error.tool_identifier == "test_tool"
    assert "Tool validation error: Invalid input" in str(validation_error)

    # ToolExecutionError
    execution_error = ToolExecutionError("test_tool", "Execution failed")
    assert execution_error.tool_identifier == "test_tool"
    assert "Tool execution error: Execution failed" in str(execution_error)

    # ToolTimeoutError
    timeout_error = ToolTimeoutError("test_tool")
    assert timeout_error.tool_identifier == "test_tool"
    assert "Tool execution timed out" in str(timeout_error)


if __name__ == "__main__":
    pytest.main([__file__])
