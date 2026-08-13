"""
Research Agent abstraction for executing research tasks.
"""
from abc import ABC, abstractmethod

from deep_research.context.context_builder import AgentContext
from deep_research.domain.research.research_task import ResearchTask
from deep_research.tools.tool import ToolResult


class ResearchAgent(ABC):
    """
    Abstract base class for research agents that execute individual research tasks
    using appropriate tools.
    """

    @abstractmethod
    async def execute_task(
        self, task: ResearchTask, context: AgentContext | None = None
    ) -> ToolResult:
        """
        Execute a research task using an appropriate tool selected based on the task.

        Args:
            task: The research task to execute

        Returns:
            The result of the tool execution
        """
        pass
