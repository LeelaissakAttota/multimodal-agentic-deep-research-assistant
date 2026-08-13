"""
Research Result domain model.
"""
from datetime import UTC, datetime
from typing import Any, Dict, List
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

class ResearchResult(BaseModel):
    """
    Represents the final outcome of a research session.
    """
    id: UUID = Field(default_factory=uuid4, description="Unique identifier for the research result")
    request_id: UUID = Field(..., description="ID of the research request that led to this result")
    answer: str = Field(..., description="The synthesized answer to the research question")
    summary: str = Field(default="", description="Executive summary of the research findings")
    sources_consulted: List[UUID] = Field(default_factory=list, description="List of source IDs consulted during research")
    evidence_used: List[UUID] = Field(default_factory=list, description="List of evidence IDs used to support claims")
    claims_made: List[UUID] = Field(default_factory=list, description="List of claim IDs made in the result")
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0, description="Overall confidence in the result")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When the result was generated",
    )
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
