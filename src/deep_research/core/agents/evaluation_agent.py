"""
Evaluation/Reflection Agent abstraction for evaluating research quality.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

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
        self.decision = decision
        self.confidence = confidence
        self.reasoning = reasoning
        self.gaps = gaps or []
        self.metadata = metadata or {}


class EvaluationAgent(ABC):
    """
    Abstract base class for evaluation agents that assess the quality of research
    and determine if further iterations are needed.
    """

    @abstractmethod
    async def evaluate(
        self, state: ResearchState, plan: ResearchPlan, analysis_result: Dict[str, Any]
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
    Deterministic implementation of EvaluationAgent for Phase 2.
    Implements a simple evaluation based on task completion and evidence.
    """

    async def evaluate(
        self, state: ResearchState, plan: ResearchPlan, analysis_result: Dict[str, Any]
    ) -> EvaluationResult:
        """
        Perform deterministic evaluation.
        For Phase 2, we check if all tasks are completed and if we have sufficient evidence.
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
