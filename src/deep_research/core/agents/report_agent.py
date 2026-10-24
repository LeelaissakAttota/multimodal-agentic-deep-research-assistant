"""
Report Agent abstraction for generating final research reports.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List

from deep_research.context.context_builder import AgentContext
from deep_research.domain.research.research_state import ResearchState
from deep_research.evidence.claim import Claim
from deep_research.evidence.scoring import claim_scores, score_citations


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


def report_title(state: ResearchState, max_len: int = 120) -> str:
    """Human-readable title: the research objective when known, else the request id."""
    objective = state.metadata.get("objective") if isinstance(state.metadata, dict) else None
    if isinstance(objective, str) and objective.strip():
        text = " ".join(objective.split())
        if len(text) > max_len:
            text = text[: max_len - 1].rstrip() + "…"
        return f"Research Report: {text}"
    return f"Research Report for Request {state.request_id}"


class DeterministicReportAgent(ReportAgent):
    """
    Deterministic implementation of ReportAgent for Phase 2.
    Creates a simple report based on the analysis results.

    With a ``verifier`` (``deep_research.verification.ClaimVerifier``) every
    cited claim of a live report is fact-checked against the excerpts of the
    sources it cites and a "Claim verification" section is added.
    """

    def __init__(self, verifier: Any | None = None) -> None:
        self.verifier = verifier

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
            "title": report_title(state),
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
            excerpts = _apply_live_findings(report, findings)
            if report.get("citations"):
                await self._verify(report, excerpts)

        return report

    async def _verify(self, report: Dict[str, Any], excerpts: Dict[int, str]) -> None:
        from deep_research.verification.contradictions import contradictions_markdown, find_contradictions
        from deep_research.verification.fact_check import ClaimVerifier, extract_claims, verification_markdown

        verifier = self.verifier or ClaimVerifier()
        claims = extract_claims(str(report.get("content") or ""))
        result = await verifier.verify(claims, excerpts)
        report["verification"] = result
        conflicts = find_contradictions(claims, result)
        report["contradictions"] = conflicts
        insert_section(report, verification_markdown(result))
        insert_section(report, contradictions_markdown(conflicts, claim_count=len(claims)))
        from deep_research.verification.charts import build_charts, charts_markdown

        report["charts"] = build_charts(report)
        if report["charts"]:
            insert_section(report, charts_markdown(report["charts"]))


_CITE = r"\[(\d+(?:\s*[,\u2013-]\s*\d+)*)\]"
_HEADING_PREFIXES = (
    "understand the fundamentals of", "identify recent trends and advances in",
    "determine the significance and impact of", "extract evidence from the provided",
    "gather information to address the gap:",
)


_TRANSLATE = str.maketrans({"\u3010": "[", "\u3011": "]", "\uff3b": "[", "\uff3d": "]", "\u202f": " ",
                            "\u00a0": " ", "\u2009": " ", "\u2011": "-", "\u2010": "-"})


def normalize_text(text: str) -> str:
    """Map full-width citation brackets and exotic spaces/hyphens from LLM output to ASCII."""
    import re

    text = text.translate(_TRANSLATE)
    # "[1][4]" -> "[1, 4]" so adjacent markers renumber as one group
    return re.sub(r"\](\s*)\[(?=\d)", ", ", text)


def section_heading(finding: Dict[str, Any]) -> str:
    """Concise heading: planner section label, else a keyword heading derived from the task objective."""
    section = finding.get("section")
    if isinstance(section, str) and section.strip():
        return section.strip()
    objective = str(finding.get("objective") or "").strip()
    lowered = objective.lower()
    for prefix, label in zip(_HEADING_PREFIXES, ("Fundamentals", "Recent developments", "Significance and impact",
                                                 "Attachment", "Follow-up")):
        if lowered.startswith(prefix):
            return label
    words = objective.split()
    return " ".join(words[:6]) + ("..." if len(words) > 6 else "") if words else "Findings"


def insert_section(report: Dict[str, Any], section: str) -> None:
    """Insert a Markdown section before the report's "## Sources" list (or append when there is none)."""
    content = str(report.get("content") or "")
    marker = "\n\n## Sources\n"
    if marker in content:
        head, tail = content.split(marker, 1)
        report["content"] = f"{head}\n\n{section}{marker}{tail}"
    else:
        report["content"] = f"{content}\n\n{section}" if content else section


def _apply_live_findings(report: Dict[str, Any], findings: List[Any]) -> Dict[int, str]:
    """Render live-mode syntheses with globally numbered citations.

    Each task's ``[n]`` markers refer to its own source list; they are
    renumbered into one report-wide citation list so ``[n]`` in the content
    always matches ``citations[n-1]``. Attachment sources (no URL) are cited
    by label and listed first in their section's source order.
    """
    builder = _CitationBuilder()
    sections: List[str] = []
    attachments: List[str] = []
    models: List[str] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        if finding.get("kind") == "attachment":
            attachments.append(_attachment_line(finding))
            continue
        mapping = builder.map_sources(finding.get("sources") or [])
        summary = builder.renumber(normalize_text(str(finding.get("summary") or "").strip()), mapping)
        sections.append(f"## {section_heading(finding)}\n\n{summary}")
        model_id = finding.get("model_id")
        if isinstance(model_id, str) and model_id and model_id not in models:
            models.append(model_id)
    if not sections and not attachments:
        return {}
    parts: List[str] = []
    if attachments:
        parts.append("## Attached files\n\n" + "\n".join(attachments))
    parts.extend(sections)
    citations = builder.citations
    score_citations(citations)
    if citations:
        parts.append("## Sources\n\n" + "\n".join(_citation_line(c) for c in citations))
    report["content"] = "\n\n".join(parts)
    report["citations"] = citations
    report["sources"] = citations
    report["claim_scores"] = claim_scores(report["content"], citations)
    report["models_used"] = models
    report["synthesis"] = "llm" if models else "snippets"
    report["attachments"] = [
        {**f["attachment"], "title": section_heading(f)}
        for f in findings
        if isinstance(f, dict) and f.get("kind") == "attachment" and isinstance(f.get("attachment"), dict)
    ]
    return builder.excerpts


class _CitationBuilder:
    def __init__(self) -> None:
        self.citations: List[Dict[str, Any]] = []
        self._index: Dict[str, int] = {}
        self.excerpts: Dict[int, str] = {}

    def map_sources(self, sources: List[Any]) -> Dict[int, int]:
        mapping: Dict[int, int] = {}
        for local, source in enumerate(sources, start=1):
            if not isinstance(source, dict):
                continue
            url = source.get("url")
            key = str(url) if url else f"attachment:{source.get('title')}"
            if not url and source.get("kind") != "attachment":
                continue
            if key not in self._index:
                entry: Dict[str, Any] = {"number": len(self.citations) + 1, "source_id": source.get("source_id"),
                                         "url": str(url) if url else None, "title": source.get("title") or url}
                if source.get("kind") == "attachment":
                    entry["kind"] = "attachment"
                    entry["page"] = source.get("page")
                elif source.get("source_type"):
                    entry["kind"] = source["source_type"]
                    entry.update({k: source[k] for k in _CITATION_EXTRAS if source.get(k)})
                self.citations.append(entry)
                self._index[key] = len(self.citations)
            excerpt = source.get("excerpt")
            if isinstance(excerpt, str) and excerpt.strip():
                number = self._index[key]
                self.excerpts[number] = (self.excerpts.get(number, "") + "\n" + excerpt).strip()[:4000]
                self.citations[number - 1]["excerpt"] = self.excerpts[number][:1200]
            mapping[local] = self._index[key]
        return mapping

    @staticmethod
    def renumber(summary: str, mapping: Dict[int, int]) -> str:
        import re

        def _sub(match: "re.Match[str]") -> str:
            mapped = [str(mapping[n]) for n in (int(x) for x in re.findall(r"\d+", match.group(0))) if n in mapping]
            return "[" + ", ".join(dict.fromkeys(mapped)) + "]" if mapped else ""

        return re.sub(_CITE, _sub, summary)


_CITATION_EXTRAS = ("tool", "provider", "authors", "year", "venue", "image_url", "license")
_KIND_LABELS = {"academic": "paper", "video": "video transcript", "social": "community discussion",
                "image": "image"}


def _citation_line(citation: Dict[str, Any]) -> str:
    if citation.get("kind") == "attachment":
        return f"[{citation['number']}] {citation['title']} - user attachment"
    label = _KIND_LABELS.get(str(citation.get("kind") or ""))
    if label is None:
        return f"[{citation['number']}] {citation['title']} - {citation['url']}"
    details = [label]
    if citation.get("authors"):
        details.append(str(citation["authors"]).split(",")[0] + (" et al." if "," in str(citation["authors"]) else ""))
    if citation.get("year"):
        details.append(str(citation["year"]))
    if citation.get("venue"):
        details.append(str(citation["venue"]))
    if citation.get("license"):
        details.append(str(citation["license"]))
    return f"[{citation['number']}] {citation['title']} ({', '.join(details)}) - {citation['url']}"


def _attachment_line(finding: Dict[str, Any]) -> str:
    raw_info = finding.get("attachment")
    info: Dict[str, Any] = raw_info if isinstance(raw_info, dict) else {}
    label = section_heading(finding)
    bits: List[str] = []
    if info.get("page_count"):
        bits.append(f"{info['page_count']} page(s)")
    if info.get("ocr_engine"):
        bits.append(f"text read with local OCR, {info['ocr_engine']}")
    if info.get("has_text") is False:
        bits.append(str(info.get("note") or "no extractable text"))
        bits.append(f"{info.get('image_count') or 0} embedded image(s)")
    detail = f" ({'; '.join(bits)})" if bits else ""
    summary = str(finding.get("summary") or "").strip()
    show = info.get("has_text") is False or (info.get("ocr_engine") and info.get("page_count") is None)
    extra = f"\n\n{_flatten_markdown(summary)}" if show and summary else ""
    return f"- {label}{detail}{extra}"


def _flatten_markdown(text: str) -> str:
    """Keep model-written headings/rules from breaking the report outline (``### X`` -> ``**X**``)."""
    import re

    text = re.sub(r"(?m)^\s*#{1,6}\s*(.+?)\s*#*\s*$", r"**\1**", text)
    return re.sub(r"(?m)^\s*(?:-{3,}|\*{3,}|_{3,})\s*$\n?", "", text)
