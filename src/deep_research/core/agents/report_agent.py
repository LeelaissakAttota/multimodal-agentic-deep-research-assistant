"""
Report Agent abstraction for generating final research reports.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List

from deep_research.context.context_builder import AgentContext
from deep_research.domain.research.research_state import ResearchState
from deep_research.evidence.claim import Claim


class ReportAgent(ABC):
    """
    Abstract base class for report agents that generate final traceable reports
    from validated evidence and claims.
    """

    @abstractmethod
    async def generate_report(
        self,
        state: ResearchState,
        analysis_result: Dict[str, Any],
        context: AgentContext | None = None,
    ) -> Dict[str, Any]:
        """
        Generate a final report from the research state and analysis results.

        Args:
            state: The final research state
            analysis_result: The results from the analysis/synthesis phase

        Returns:
            A dictionary representing the report, which may include:
            - title: Report title
            - content: Main content of the report
            - citations: List of citations
            - metadata: Additional metadata
        """
        pass


class DeterministicReportAgent(ReportAgent):
    """
    Deterministic implementation of ReportAgent for Phase 2.
    Creates a simple report based on the analysis results.
    """

    async def generate_report(
        self,
        state: ResearchState,
        analysis_result: Dict[str, Any],
        context: AgentContext | None = None,
    ) -> Dict[str, Any]:
        """
        Generate a deterministic report.
        For Phase 2, we create a simple report with the analysis summary and a list of claims.
        """
        claims: List[Claim] = analysis_result.get("claims", [])
        summary = analysis_result.get("summary", "No analysis available.")

        # Create a simple report
        report = {
            "title": f"Research Report for Request {state.request_id}",
            "content": summary,
            "claims": [
                {
                    "id": str(claim.id),
                    "text": claim.text,
                    "evidence_ids": [str(eid) for eid in claim.supported_by],
                    "confidence": claim.confidence,
                }
                for claim in claims
            ],
            "evidence_gathered": [str(eid) for eid in state.gathered_evidence],
            "sources_consulted": [str(sid) for sid in state.consulted_sources],
            "iteration_count": state.iteration_number,
            "status": state.status,
        }

        # If we have citations in the analysis result, we can include them
        # For now, we don't generate citations in the analysis phase, so we leave it empty.
        report["citations"] = []

        return report
