from datetime import UTC, datetime
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class EvaluationRecord(BaseModel):
    """Immutable-by-convention record of one evaluation/reflection step."""

    iteration_number: int = Field(..., ge=0)
    decision: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    reasoning: Optional[str] = None
    gaps: List[str] = Field(default_factory=list)


class ResearchState(BaseModel):
    """
    Represents the current state of a research session.
    """
    id: UUID = Field(default_factory=uuid4, description="Unique identifier for the research session")
    request_id: UUID = Field(..., description="ID of the original research request")
    current_plan_id: Optional[UUID] = Field(default=None, description="ID of the currently active research plan")
    plan_history: List[UUID] = Field(default_factory=list, description="List of plan IDs that have been used in this session")
    completed_task_ids: List[UUID] = Field(default_factory=list, description="List of task IDs that have been successfully completed")
    failed_task_ids: List[UUID] = Field(default_factory=list, description="List of task IDs that have failed")
    consulted_sources: List[UUID] = Field(default_factory=list, description="List of source IDs that have been consulted")
    gathered_evidence: List[UUID] = Field(default_factory=list, description="List of evidence IDs that have been gathered")
    generated_claims: List[UUID] = Field(default_factory=list, description="List of claim IDs that have been generated")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When the research session started",
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When the state was last updated",
    )
    status: str = Field(
        default="initialized",
        description=(
            "Current status of the research session (initialized, planning, researching, "
            "analyzing, evaluating, reporting, completed, failed)"
        ),
    )
    error: Optional[str] = Field(default=None, description="Error message if the research session failed")
    iteration_number: int = Field(default=0, description="Current iteration number of the research loop")
    evaluation_result: Optional[str] = Field(
        default=None,
        description=(
            "Result of the last evaluation (e.g., CONTINUE, COMPLETE, BLOCKED, FAILED)"
        ),
    )
    last_evaluation_gaps: List[str] = Field(
        default_factory=list,
        description="Gaps identified in the last evaluation",
    )
    last_evaluation_reasoning: Optional[str] = Field(
        default=None,
        description="Reasoning from the last evaluation",
    )
    last_evaluation_confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Confidence score from the last evaluation",
    )
    evaluation_history: List[EvaluationRecord] = Field(
        default_factory=list,
        description="Ordered evaluation/reflection records for the research loop",
    )
    status_history: List[str] = Field(
        default_factory=lambda: ["initialized"],
        description="Ordered states visited by the research state graph",
    )
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
