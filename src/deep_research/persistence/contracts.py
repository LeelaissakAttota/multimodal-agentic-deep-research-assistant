"""Provider-independent persistence contracts for Phase 5 research state."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from deep_research.domain.research.research_plan import ResearchPlan
from deep_research.domain.research.research_state import ResearchState
from deep_research.domain.research_request import ResearchRequest
from deep_research.evidence.claim import Claim
from deep_research.evidence.evidence import Evidence
from deep_research.evidence.source import Source
from deep_research.errors.base import DeepResearchError

CURRENT_SCHEMA_VERSION = 1


class PersistenceError(DeepResearchError):
    """Normalized failure raised by a research persistence adapter."""


class PersistenceNotFoundError(PersistenceError):
    """Raised when a requested research session does not exist."""


class PersistenceConflictError(PersistenceError):
    """Raised when a write would mutate immutable historical research data."""


class PersistenceSchemaError(PersistenceError):
    """Raised when persisted data uses an unsupported schema version."""


class SecretDataError(PersistenceError):
    """Raised when configuration-like secret material is found in persisted data."""


class ResearchSessionSnapshot(BaseModel):
    """Recoverable aggregate for one research session.

    Evidence, source, and claim objects are optional because older tools may return
    only their stable identifiers. When objects are supplied, their provenance links
    are validated before they reach storage.
    """

    schema_version: int = Field(default=CURRENT_SCHEMA_VERSION, ge=1)
    request: ResearchRequest
    state: ResearchState
    plans: list[ResearchPlan] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    evidence_task_links: dict[UUID, list[UUID]] = Field(default_factory=dict)
    report_metadata: dict[str, Any] = Field(default_factory=dict)
    saved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_aggregate_links(self) -> ResearchSessionSnapshot:
        if self.schema_version != CURRENT_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported research snapshot schema version: {self.schema_version}"
            )
        if self.request.id != self.state.request_id:
            raise ValueError("Snapshot request ID does not match research state")

        plan_ids, task_ids = self._validate_plans()
        self._validate_state_plan_and_task_links(plan_ids, task_ids)
        source_ids = self._unique_ids(self.sources, "source")
        evidence_ids = self._unique_ids(self.evidence, "evidence")
        self._unique_ids(self.claims, "claim")
        self._validate_evidence_sources(source_ids)
        self._validate_claim_evidence(evidence_ids)
        self._validate_task_links(evidence_ids, task_ids)
        return self

    def _validate_plans(self) -> tuple[set[UUID], set[UUID]]:
        plan_ids: set[UUID] = set()
        task_ids: set[UUID] = set()
        for plan in self.plans:
            if plan.request_id != self.request.id:
                raise ValueError("Persisted plan belongs to a different request")
            if plan.id in plan_ids:
                raise ValueError(f"Duplicate plan ID in snapshot: {plan.id}")
            plan_ids.add(plan.id)
            for task in plan.tasks:
                if task.plan_id != plan.id:
                    raise ValueError("Persisted task belongs to a different plan")
                if task.id in task_ids:
                    raise ValueError(f"Duplicate task ID in snapshot: {task.id}")
                task_ids.add(task.id)
        return plan_ids, task_ids

    def _validate_state_plan_and_task_links(
        self, plan_ids: set[UUID], task_ids: set[UUID]
    ) -> None:
        referenced_plan_ids = set(self.state.plan_history)
        if self.state.current_plan_id is not None:
            referenced_plan_ids.add(self.state.current_plan_id)
        missing_plans = referenced_plan_ids.difference(plan_ids)
        if missing_plans:
            raise ValueError(
                "Research state references missing plan: "
                f"{', '.join(sorted(str(item) for item in missing_plans))}"
            )

        referenced_task_ids = set(self.state.completed_task_ids)
        referenced_task_ids.update(self.state.failed_task_ids)
        missing_tasks = referenced_task_ids.difference(task_ids)
        if missing_tasks:
            raise ValueError(
                "Research state references missing task: "
                f"{', '.join(sorted(str(item) for item in missing_tasks))}"
            )

    def _validate_evidence_sources(self, source_ids: set[UUID]) -> None:
        for item in self.evidence:
            if item.source_id not in source_ids:
                raise ValueError(
                    f"Evidence {item.id} references missing source {item.source_id}"
                )

    def _validate_claim_evidence(self, evidence_ids: set[UUID]) -> None:
        for claim in self.claims:
            missing = set(claim.supported_by).difference(evidence_ids)
            if missing:
                raise ValueError(
                    f"Claim {claim.id} references missing evidence: "
                    f"{', '.join(sorted(str(item) for item in missing))}"
                )

    def _validate_task_links(
        self, evidence_ids: set[UUID], task_ids: set[UUID]
    ) -> None:
        for evidence_id, linked_task_ids in self.evidence_task_links.items():
            if evidence_id not in evidence_ids:
                raise ValueError(
                    f"Task link references missing evidence {evidence_id}"
                )
            missing_tasks = set(linked_task_ids).difference(task_ids)
            if missing_tasks:
                raise ValueError(
                    "Evidence task link references missing task: "
                    f"{', '.join(sorted(str(item) for item in missing_tasks))}"
                )

    @staticmethod
    def _unique_ids(items: list[Any], label: str) -> set[UUID]:
        identifiers = [item.id for item in items]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError(f"Duplicate {label} ID in snapshot")
        return set(identifiers)


@runtime_checkable
class ResearchSessionRepository(Protocol):
    """Storage boundary used by memory and orchestration services."""

    def initialize(self) -> None:
        """Create or validate the deterministic storage schema."""

    def save_session(self, snapshot: ResearchSessionSnapshot) -> None:
        """Atomically create or update a recoverable session aggregate."""

    def load_session(self, session_id: UUID) -> ResearchSessionSnapshot:
        """Load a complete session aggregate or raise a not-found error."""

    def list_sessions(self, limit: int = 100) -> list[ResearchSessionSnapshot]:
        """Return a bounded, deterministic list of saved sessions."""

    def delete_session(self, session_id: UUID) -> bool:
        """Delete one session, returning whether it existed."""
