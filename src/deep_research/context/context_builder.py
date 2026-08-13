"""Bounded and traceable working-context construction."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field

from deep_research.domain.research.research_state import EvaluationRecord, ResearchState
from deep_research.domain.research_request import ResearchRequest
from deep_research.memory.research_memory import MemoryQuery, MemoryRecord, ResearchMemory


class ContextPolicy(BaseModel):
    """Deterministic text and item limits for one agent working context."""

    max_characters: int = Field(default=4_000, ge=100, le=100_000)
    max_evidence: int = Field(default=8, ge=0, le=100)
    max_reflections: int = Field(default=3, ge=0, le=25)
    max_gaps: int = Field(default=10, ge=0, le=100)
    max_task_ids: int = Field(default=25, ge=0, le=250)
    max_candidate_sessions: int = Field(default=25, ge=1, le=100)
    candidate_multiplier: int = Field(default=3, ge=1, le=10)


class ContextEvidence(BaseModel):
    """A bounded evidence excerpt retaining all provenance identifiers."""

    session_id: UUID
    evidence_id: UUID
    source_id: UUID
    task_ids: list[UUID] = Field(default_factory=list)
    claim_ids: list[UUID] = Field(default_factory=list)
    content_excerpt: str
    source_title: str | None = None
    source_url: str | None = None
    relevance_score: int = Field(ge=0)
    truncated: bool = False


class AgentContext(BaseModel):
    """Small reproducible view supplied to one agent invocation.

    `text_character_count` covers objective, task, gap, reflection, and evidence
    text. Structural identifiers and source locators are separately item-bounded.
    """

    session_id: UUID
    request_id: UUID
    objective: str
    current_task: str | None = None
    iteration_number: int
    status: str
    evaluation_gaps: list[str] = Field(default_factory=list)
    reflections: list[EvaluationRecord] = Field(default_factory=list)
    completed_task_ids: list[UUID] = Field(default_factory=list)
    failed_task_ids: list[UUID] = Field(default_factory=list)
    evidence: list[ContextEvidence] = Field(default_factory=list)
    omitted_evidence_count: int = Field(default=0, ge=0)
    text_character_count: int = Field(default=0, ge=0)
    selection_trace: list[str] = Field(default_factory=list)


class ResearchContextBuilder:
    """Selects relevant durable memory into bounded active working context."""

    def __init__(self, memory: ResearchMemory, policy: ContextPolicy | None = None) -> None:
        self.memory = memory
        self.policy = policy or ContextPolicy()

    def build(
        self,
        request: ResearchRequest,
        state: ResearchState,
        current_task: str | None = None,
    ) -> AgentContext:
        character_count = 0
        objective, used = self._take(request.objective, self.policy.max_characters)
        character_count += used
        remaining = self.policy.max_characters - character_count

        bounded_task: str | None = None
        if current_task and remaining > 0:
            bounded_task, used = self._take(current_task, remaining)
            character_count += used
            remaining -= used

        gaps: list[str] = []
        for gap in state.last_evaluation_gaps[: self.policy.max_gaps]:
            if remaining <= 0:
                break
            bounded_gap, used = self._take(gap, remaining)
            gaps.append(bounded_gap)
            character_count += used
            remaining -= used

        reflections: list[EvaluationRecord] = []
        reflection_candidates = state.evaluation_history[-self.policy.max_reflections :]
        if self.policy.max_reflections == 0:
            reflection_candidates = []
        for record in reflection_candidates:
            if remaining <= 0:
                break
            bounded_record, used = self._bound_reflection(record, remaining)
            reflections.append(bounded_record)
            character_count += used
            remaining -= used

        records = self._query_memory(request.objective, current_task)

        selected: list[ContextEvidence] = []
        trace: list[str] = []
        for record in records:
            if len(selected) >= self.policy.max_evidence or remaining <= 0:
                break
            excerpt, used = self._take(record.evidence.content, remaining)
            if not excerpt:
                break
            truncated = len(excerpt) < len(record.evidence.content)
            selected.append(
                ContextEvidence(
                    session_id=record.session_id,
                    evidence_id=record.evidence.id,
                    source_id=record.source.id,
                    task_ids=record.task_ids,
                    claim_ids=[claim.id for claim in record.claims],
                    content_excerpt=excerpt,
                    source_title=record.source.title,
                    source_url=str(record.source.url) if record.source.url else None,
                    relevance_score=record.relevance_score,
                    truncated=truncated,
                )
            )
            trace.append(
                f"evidence:{record.evidence.id}:source:{record.source.id}:score:{record.relevance_score}"
            )
            character_count += used
            remaining -= used

        return AgentContext(
            session_id=state.id,
            request_id=request.id,
            objective=objective,
            current_task=bounded_task,
            iteration_number=state.iteration_number,
            status=state.status,
            evaluation_gaps=gaps,
            reflections=reflections,
            completed_task_ids=state.completed_task_ids[-self.policy.max_task_ids :]
            if self.policy.max_task_ids
            else [],
            failed_task_ids=state.failed_task_ids[-self.policy.max_task_ids :]
            if self.policy.max_task_ids
            else [],
            evidence=selected,
            omitted_evidence_count=max(0, len(records) - len(selected)),
            text_character_count=character_count,
            selection_trace=trace,
        )

    def _query_memory(
        self, objective: str, current_task: str | None
    ) -> list[MemoryRecord]:
        if self.policy.max_evidence == 0:
            return []
        candidate_limit = self.policy.max_evidence * self.policy.candidate_multiplier
        return self.memory.query(
            MemoryQuery(
                objective=objective,
                current_task=current_task,
                limit=min(candidate_limit, 100),
                max_sessions=self.policy.max_candidate_sessions,
            )
        )

    @staticmethod
    def _take(value: str, remaining: int) -> tuple[str, int]:
        if remaining <= 0:
            return "", 0
        bounded = value[:remaining]
        return bounded, len(bounded)

    @classmethod
    def _bound_reflection(
        cls, record: EvaluationRecord, remaining: int
    ) -> tuple[EvaluationRecord, int]:
        used = 0
        reasoning: str | None = None
        if record.reasoning and remaining > 0:
            reasoning, consumed = cls._take(record.reasoning, remaining)
            used += consumed
            remaining -= consumed
        gaps: list[str] = []
        for gap in record.gaps:
            if remaining <= 0:
                break
            bounded_gap, consumed = cls._take(gap, remaining)
            gaps.append(bounded_gap)
            used += consumed
            remaining -= consumed
        return (
            EvaluationRecord(
                iteration_number=record.iteration_number,
                decision=record.decision,
                confidence=record.confidence,
                reasoning=reasoning,
                gaps=gaps,
            ),
            used,
        )
