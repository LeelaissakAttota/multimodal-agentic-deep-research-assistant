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

        report["citations"] = []
        findings = state.metadata.get("live_findings")
        if isinstance(findings, list) and findings:
            _apply_live_findings(report, findings)

        return report


def _apply_live_findings(report: Dict[str, Any], findings: List[Any]) -> None:
    """Render live-mode syntheses with globally numbered, real-URL citations.

    Each task's ``[n]`` markers refer to its own source list; they are
    renumbered into one report-wide citation list so ``[n]`` in the content
    always matches ``citations[n-1]``.
    """
    import re

    citations: List[Dict[str, Any]] = []
    index_by_url: Dict[str, int] = {}
    sections: List[str] = []
    models: List[str] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        local_to_global: Dict[int, int] = {}
        for local, source in enumerate(finding.get("sources") or [], start=1):
            if not isinstance(source, dict) or not source.get("url"):
                continue
            url = str(source["url"])
            if url not in index_by_url:
                citations.append(
                    {
                        "number": len(citations) + 1,
                        "source_id": source.get("source_id"),
                        "url": url,
                        "title": source.get("title") or url,
                    }
                )
                index_by_url[url] = len(citations)
            local_to_global[local] = index_by_url[url]
        summary = str(finding.get("summary") or "").strip()

        def _renumber(match: "re.Match[str]", mapping: Dict[int, int] = local_to_global) -> str:
            numbers = [int(n) for n in re.findall(r"\d+", match.group(0))]
            mapped = [str(mapping[n]) for n in numbers if n in mapping]
            return "[" + ", ".join(mapped) + "]" if mapped else ""

        summary = re.sub(r"\[(\d+(?:\s*[,\u2013-]\s*\d+)*)\]", _renumber, summary)
        heading = str(finding.get("objective") or "Findings")
        sections.append(f"## {heading}\n\n{summary}")
        model_id = finding.get("model_id")
        if isinstance(model_id, str) and model_id and model_id not in models:
            models.append(model_id)
    if not sections:
        return
    source_lines = "\n".join(f"[{c['number']}] {c['title']} - {c['url']}" for c in citations)
    report["content"] = "\n\n".join(sections) + ("\n\n## Sources\n\n" + source_lines if source_lines else "")
    report["citations"] = citations
    report["sources"] = citations
    report["models_used"] = models
    report["synthesis"] = "llm" if models else "snippets"
