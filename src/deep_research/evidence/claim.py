"""
Claim domain model.
"""
from datetime import UTC, datetime
from typing import Any, List
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class Claim(BaseModel):
    """
    Represents a statement or assertion that is supported by evidence.
    """
    id: UUID = Field(default_factory=uuid4, description="Unique identifier for the claim")
    text: str = Field(..., description="The claim text")
    supported_by: List[UUID] = Field(default_factory=list, description="List of evidence IDs that support this claim")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Confidence score for the claim")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When the claim was made",
    )
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
