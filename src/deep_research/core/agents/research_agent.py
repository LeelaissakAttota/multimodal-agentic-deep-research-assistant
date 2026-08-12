"""
Research Agent abstraction for executing research tasks.
"""
from abc import ABC, abstractmethod
from uuid import UUID

from deep_research.domain.research.research_task import ResearchTask
from deep_research.tools.tool import ToolRequest, ToolResult


class ResearchAgent(ABC):
    """
    Abstract base class for research agents that execute individual research tasks
    using appropriate tools.
    """

    @abstractmethod
    async def execute_task(
        self, task: ResearchTask, tool_request: ToolRequest
    ) -> ToolResult:
        """
        Execute a research task using the specified tool.

        Args:
            task: The research task to execute
            tool_request: The tool request containing tool name and parameters

        Returns:
            The result of the tool execution
        """
        pass


class DeterministicResearchAgent(ResearchAgent):
    """
    Deterministic implementation of ResearchAgent for Phase 2.
    Uses fake tools that return deterministic results for testing.
    """

    async def execute_task(
        self, task: ResearchTask, tool_request: ToolRequest
    ) -> ToolResult:
        """
        Execute a research task with deterministic fake tool results.
        """
        # For Phase 2, we simulate tool execution based on the tool name
        tool_name = tool_request.tool_name

        if tool_name == "web_search":
            # Simulate web search returning some evidence
            return ToolResult(
                success=True,
                output={
                    "evidence_ids": [UUID(int=1), UUID(int=2)],
                    "source_ids": [UUID(int=10), UUID(int=11)],
                    "summary": f"Found information about {task.objective}",
                },
                tool_name=tool_name,
                execution_time_ms=100.0,
            )
        elif tool_name == "analysis":
            # Simulate analysis tool
            return ToolResult(
                success=True,
                output={
                    "evidence_ids": [UUID(int=3)],
                    "source_ids": [UUID(int=12)],
                    "summary": f"Analysis of {task.objective}",
                },
                tool_name=tool_name,
                execution_time_ms=50.0,
            )
        else:
            # Default fake tool
            return ToolResult(
                success=True,
                output={"summary": f"Task {task.description} completed"},
                tool_name=tool_name,
                execution_time_ms=50.0,
            )
