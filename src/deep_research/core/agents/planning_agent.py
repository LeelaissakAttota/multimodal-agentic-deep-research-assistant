"""
Planning Agent abstraction for decomposing research requests into tasks.
"""
from abc import ABC, abstractmethod
from uuid import UUID

from deep_research.context.context_builder import AgentContext
from deep_research.domain.research_request import ATTACHMENT_TOOLS, ResearchRequest
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
        self,
        request: ResearchRequest,
        state: ResearchState,
        context: AgentContext | None = None,
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
    Deterministic implementation of PlanningAgent for Phases 2 and 4.
    Creates tasks based on the research request and evaluation gaps.
    """

    async def create_plan(
        self,
        request: ResearchRequest,
        state: ResearchState,
        context: AgentContext | None = None,
    ) -> ResearchPlan:
        """
        Create a plan from evaluation gaps, or an initial request-based plan.
        """
        gaps = list(
            dict.fromkeys(
                gap.strip() for gap in state.last_evaluation_gaps if gap.strip()
            )
        )
        if gaps:
            tasks = [
                ResearchTask(
                    plan_id=UUID(int=0),  # Replaced after plan creation
                    description=f"Address gap: {gap}",
                    objective=f"Gather information to address the gap: {gap}",
                    assigned_tool="web_search",
                    tool_input={"query": gap, "limit": 5},
                )
                for gap in gaps
            ]
        else:
            tasks = [
                ResearchTask(
                    plan_id=UUID(int=0),  # Replaced after plan creation
                    description=f"Gather background information on: {request.objective}",
                    objective=f"Understand the fundamentals of {request.objective}",
                    assigned_tool="web_search",
                    tool_input={"query": request.objective, "limit": 5},
                ),
                ResearchTask(
                    plan_id=UUID(int=0),  # Replaced after plan creation
                    description=f"Find recent developments in: {request.objective}",
                    objective=f"Identify recent trends and advances in {request.objective}",
                    assigned_tool="web_search",
                    tool_input={"query": f"recent developments {request.objective}", "limit": 5},
                ),
                ResearchTask(
                    plan_id=UUID(int=0),  # Replaced after plan creation
                    description=f"Analyze implications of: {request.objective}",
                    objective=f"Determine the significance and impact of {request.objective}",
                    assigned_tool="web_search",
                    tool_input={
                        "query": f"implications and impact of {request.objective}",
                        "limit": 5,
                    },
                ),
            ]

            tasks.extend(_attachment_tasks(request))

        plan = ResearchPlan(
            request_id=request.id,
            objective=request.objective,
            tasks=tasks,
        )

        for task in plan.tasks:
            task.plan_id = plan.id

        return plan


def _attachment_tasks(request: ResearchRequest) -> list[ResearchTask]:
    """One task per PDF/image attachment, routed to its multimodal tool."""
    tasks: list[ResearchTask] = []
    for index, attachment in enumerate(request.attachments, 1):
        tool_id, param = ATTACHMENT_TOOLS[attachment.kind]
        label = attachment.name or f"{attachment.kind} attachment {index}"
        tasks.append(
            ResearchTask(
                plan_id=UUID(int=0),
                description=f"Analyze {attachment.kind} input '{label}' for: {request.objective}",
                objective=f"Extract evidence from the provided {attachment.kind} relevant to {request.objective}",
                assigned_tool=tool_id,
                tool_input={param: attachment.data, "source_title": label},
            )
        )
    return tasks
