"""
Evaluation/Reflection Agent abstraction for evaluating research quality.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from deep_research.context.context_builder import AgentContext
from deep_research.domain.research.research_plan import ResearchPlan
from deep_research.domain.research.research_state import ResearchState


class EvaluationResult:
    """
    Represents the result of an evaluation.
    """
    def __init__(
        self,
        decision: str,  # e.g., "CONTINUE", "COMPLETE", "BLOCKED", "FAILED"
        confidence: float = 0.0,
        reasoning: Optional[str] = None,
        gaps: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        valid_decisions = {"CONTINUE", "COMPLETE", "BLOCKED", "FAILED"}
        if decision not in valid_decisions:
            raise ValueError(f"Unsupported evaluation decision: {decision}")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("Evaluation confidence must be between 0.0 and 1.0")

        self.decision = decision
        self.confidence = confidence
        self.reasoning = reasoning
        self.gaps = list(dict.fromkeys(gap.strip() for gap in gaps or [] if gap.strip()))
        self.metadata = metadata or {}


class EvaluationAgent(ABC):
    """
    Abstract base class for evaluation agents that assess the quality of research
    and determine if further iterations are needed.
    """

    @abstractmethod
    async def evaluate(
        self,
        state: ResearchState,
        plan: ResearchPlan,
        analysis_result: Dict[str, Any],
        context: AgentContext | None = None,
    ) -> EvaluationResult:
        """
        Evaluate the current state of research and decide on next steps.

        Args:
            state: Current research state
            plan: The research plan that guided the research
            analysis_result: Results from the analysis/synthesis phase

        Returns:
            An EvaluationResult indicating the decision and reasoning.
        """
        pass


class DeterministicEvaluationAgent(EvaluationAgent):
    """
    Deterministic implementation of EvaluationAgent for Phases 2 and 4.
    Implements a simple evaluation based on task completion and evidence.
    """

    async def evaluate(
        self,
        state: ResearchState,
        plan: ResearchPlan,
        analysis_result: Dict[str, Any],
        context: AgentContext | None = None,
    ) -> EvaluationResult:
        """
        Evaluate task completion and identify evidence gaps deterministically.
        """
        total_tasks = len(plan.tasks)
        completed_tasks = len(
            [t for t in plan.tasks if t.id in state.completed_task_ids]
        )
        failed_tasks = len(
            [t for t in plan.tasks if t.id in state.failed_task_ids]
        )

        # If any task failed, we might be blocked or failed depending on configuration
        if failed_tasks > 0:
            return EvaluationResult(
                decision="FAILED",
                confidence=0.9,
                reasoning=f"{failed_tasks} out of {total_tasks} tasks failed.",
                gaps=["Failed tasks need to be retried or replanned"],
            )

        # If all tasks are completed, we can consider completing
        if completed_tasks == total_tasks and total_tasks > 0:
            # Check if we have enough evidence (at least one evidence per task is a simple heuristic)
            total_evidence = len(state.gathered_evidence)
            if total_evidence >= total_tasks:
                return EvaluationResult(
                    decision="COMPLETE",
                    confidence=0.8,
                    reasoning=f"All {total_tasks} tasks completed with {total_evidence} evidence items gathered.",
                    gaps=[],
                )
            else:
                return EvaluationResult(
                    decision="CONTINUE",
                    confidence=0.7,
                    reasoning=f"All tasks completed but only {total_evidence} evidence items for {total_tasks} tasks. Need more evidence.",
                    gaps=["Insufficient evidence gathered"],
                )

        # If we have not completed all tasks and we are not failed, we continue
        # But we should also check if we are blocked by dependencies (not implemented in this simple version)
        return EvaluationResult(
            decision="CONTINUE",
            confidence=0.6,
            reasoning=f"{completed_tasks} out of {total_tasks} tasks completed. Continuing to next iteration.",
            gaps=["Some tasks still pending"],
        )
