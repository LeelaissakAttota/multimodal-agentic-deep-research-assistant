"""Bounded, provenance-preserving durable research memory."""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, Field

from deep_research.evidence.claim import Claim
from deep_research.evidence.evidence import Evidence
from deep_research.evidence.source import Source
from deep_research.persistence.contracts import (
    ResearchSessionRepository,
    ResearchSessionSnapshot,
)

_WORD_PATTERN = re.compile(r"[\w]+", re.UNICODE)
_STOP_WORDS = frozenset(
    {"a", "an", "and", "for", "in", "is", "of", "on", "or", "the", "to", "with"}
)


class MemoryQuery(BaseModel):
    """Deterministic bounded query over durable research evidence."""

    objective: str = ""
    current_task: str | None = None
    session_id: UUID | None = None
    limit: int = Field(default=10, ge=1, le=100)
    max_sessions: int = Field(default=25, ge=1, le=100)


class MemoryRecord(BaseModel):
    """Evidence plus its intact source, claim, task, and session provenance."""

    session_id: UUID
    request_id: UUID
    evidence: Evidence
    source: Source
    claims: list[Claim] = Field(default_factory=list)
    task_ids: list[UUID] = Field(default_factory=list)
    relevance_score: int = Field(default=0, ge=0)


@runtime_checkable
class ResearchMemory(Protocol):
    """Durable memory boundary consumed by context construction."""

    def store(self, snapshot: ResearchSessionSnapshot) -> None:
        """Store a session without mutating historical evidence."""

    def retrieve(self, session_id: UUID) -> ResearchSessionSnapshot:
        """Reconstruct a saved research session."""

    def query(self, query: MemoryQuery) -> list[MemoryRecord]:
        """Return bounded, relevant evidence with provenance."""


class BoundedResearchMemory:
    """Repository-backed research memory with deterministic lexical retrieval."""

    def __init__(self, repository: ResearchSessionRepository) -> None:
        self.repository = repository

    def store(self, snapshot: ResearchSessionSnapshot) -> None:
        self.repository.save_session(snapshot)

    def retrieve(self, session_id: UUID) -> ResearchSessionSnapshot:
        return self.repository.load_session(session_id).model_copy(deep=True)

    def query(self, query: MemoryQuery) -> list[MemoryRecord]:
        if query.session_id is not None:
            snapshots = [self.repository.load_session(query.session_id)]
        else:
            snapshots = self.repository.list_sessions(limit=query.max_sessions)

        objective_terms = self._terms(query.objective)
        task_terms = self._terms(query.current_task or "")
        has_filter = bool(objective_terms or task_terms)
        records: list[MemoryRecord] = []

        for snapshot in snapshots:
            sources = {source.id: source for source in snapshot.sources}
            for evidence in snapshot.evidence:
                source = sources[evidence.source_id]
                claims = sorted(
                    (
                        claim
                        for claim in snapshot.claims
                        if evidence.id in claim.supported_by
                    ),
                    key=lambda item: str(item.id),
                )
                searchable = " ".join(
                    [
                        evidence.content,
                        source.title or "",
                        source.description or "",
                        *(claim.text for claim in claims),
                    ]
                )
                searchable_terms = self._terms(searchable)
                score = len(objective_terms.intersection(searchable_terms))
                score += 2 * len(task_terms.intersection(searchable_terms))
                if has_filter and score == 0:
                    continue
                task_ids = sorted(
                    set(snapshot.evidence_task_links.get(evidence.id, [])),
                    key=str,
                )
                records.append(
                    MemoryRecord(
                        session_id=snapshot.state.id,
                        request_id=snapshot.request.id,
                        evidence=evidence,
                        source=source,
                        claims=claims,
                        task_ids=task_ids,
                        relevance_score=score,
                    )
                )

        records.sort(
            key=lambda item: (
                -item.relevance_score,
                item.evidence.extracted_at.isoformat(),
                str(item.session_id),
                str(item.evidence.id),
            )
        )
        return records[: query.limit]

    @staticmethod
    def _terms(value: str) -> set[str]:
        return {
            word
            for word in (match.group(0).casefold() for match in _WORD_PATTERN.finditer(value))
            if word not in _STOP_WORDS and len(word) > 1
        }
