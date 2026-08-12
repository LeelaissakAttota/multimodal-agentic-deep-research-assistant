"""
Analysis/Synthesis Agent abstraction for processing research evidence.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict
from uuid import UUID

from deep_research.domain.research.research_plan import ResearchPlan
from deep_research.domain.research.research_state import ResearchState
from deep_research.evidence.claim import Claim


class AnalysisAgent(ABC):
    """
    Abstract base class for analysis/synthesis agents that process raw research
    findings to extract evidence and form claims.
    """

    @abstractmethod
    async def analyze(
        self, state: ResearchState, plan: ResearchPlan
    ) -> Dict[str, Any]:
        """
        Analyze the collected evidence and synthesize findings.

        Args:
            state: Current research state containing gathered evidence
            plan: The research plan that guided the research

        Returns:
            A dictionary containing analysis results, such as:
            - claims: List of claims formed
            - evidence: List of evidence used
            - summary: Text summary of findings
            - gaps: Identified information gaps
        """
        pass


class DeterministicAnalysisAgent(AnalysisAgent):
    """
    Deterministic implementation of AnalysisAgent for Phase 2.
    Creates simple claims from evidence for demonstration.
    """

    async def analyze(
        self, state: ResearchState, plan: ResearchPlan
    ) -> Dict[str, Any]:
        """
        Perform deterministic analysis on the gathered evidence.
        For Phase 2, we create simple claims based on the evidence IDs.
        """
        # In a real implementation, we would process the actual evidence
        # For Phase 2, we'll create some mock claims and evidence

        # We'll create a claim for each piece of evidence we have
        claims = []
        for i, evidence_id in enumerate(state.gathered_evidence):
            claim = Claim(
                id=UUID(int=100 + i),  # Mock claim ID
                text=f"Claim based on evidence {evidence_id}",
                supported_by=[evidence_id],
                confidence=0.8,
            )
            claims.append(claim)

        # If we have no evidence, create a default claim
        if not claims:
            claim = Claim(
                id=UUID(int=100),
                text="No evidence gathered, unable to form claims.",
                supported_by=[],
                confidence=0.0,
            )
            claims.append(claim)

        return {
            "claims": claims,
            "evidence_gathered": state.gathered_evidence,
            "summary": f"Analysis completed. Found {len(claims)} claims based on {len(state.gathered_evidence)} evidence items.",
            "gaps": ["More evidence needed for comprehensive analysis"] if len(state.gathered_evidence) < 2 else [],
        }
