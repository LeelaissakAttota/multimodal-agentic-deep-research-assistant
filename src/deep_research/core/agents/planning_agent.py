"""
Planning Agent abstraction for decomposing research requests into tasks.
"""
from abc import ABC, abstractmethod
from uuid import UUID

from deep_research.domain.research_request import ResearchRequest
from deep_research.domain.research.research_plan import ResearchPlan
from deep_research.domain.research.research_state import ResearchState
from deep_research.domain.research.research_task import ResearchTask


class PlanningAgent(ABC):
    """
    Abstract base class for planning agents that decompose research requests
    into actionable research tasks with dependencies.
    """

    @abstractmethod
    async def create_plan(
        self, request: ResearchRequest, state: ResearchState
    ) -> ResearchPlan:
        """
        Create a research plan to fulfill the given research request.

        Args:
            request: The research request to fulfill
            state: Current research state (may contain historical context)

        Returns:
            A research plan containing tasks to execute
        """
        pass


class DeterministicPlanningAgent(PlanningAgent):
    """
    Deterministic implementation of PlanningAgent for Phase 2.
    Creates simple tasks based on the research request without external LLM calls.
    """

    async def create_plan(
        self, request: ResearchRequest, state: ResearchState
    ) -> ResearchPlan:
        """
        Create a simple research plan with tasks based on the request.
        For Phase 2, we create a fixed set of tasks for demonstration.
        """
        # In a real implementation, this would analyze the request and create appropriate tasks
        # For Phase 2, we'll create some basic tasks

        tasks = [
            ResearchTask(
                plan_id=UUID(int=0),  # Will be overridden
                description=f"Gather background information on: {request.objective}",
                objective=f"Understand the fundamentals of {request.objective}",
                assigned_tool="web_search",
                tool_input={"query": request.objective, "max_results": 5},
            ),
            ResearchTask(
                plan_id=UUID(int=0),  # Will be overridden
                description=f"Find recent developments in: {request.objective}",
                objective=f"Identify latest trends and advances in {request.objective}",
                assigned_tool="web_search",
                tool_input={"query": f"latest {request.objective} 2024", "max_results": 5},
            ),
            ResearchTask(
                plan_id=UUID(int=0),  # Will be overridden
                description=f"Analyze implications of: {request.objective}",
                objective=f"Determine the significance and impact of {request.objective}",
                assigned_tool="analysis",
                tool_input={"focus": "implications"},
            )
        ]

        # Update task plan_ids to match the plan we're creating
        plan = ResearchPlan(
            request_id=request.id,
            objective=request.objective,
            tasks=tasks,
        )

        # Now update the tasks with the correct plan_id
        for task in plan.tasks:
            task.plan_id = plan.id

        return plan
