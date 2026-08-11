"""
Claim domain model.
"""
from uuid import UUID, uuid4
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field


class Claim(BaseModel):
    """
    Represents a statement or assertion that is supported by evidence.
    """
    id: UUID = Field(default_factory=uuid4, description="Unique identifier for the claim")
    text: str = Field(..., description="The claim text")
    supported_by: List[UUID] = Field(default_factory=list, description="List of evidence IDs that support this claim")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Confidence score for the claim")
    created_at: datetime = Field(default_factory=datetime.utcnow, description="When the claim was made")
    metadata: dict = Field(default_factory=dict, description="Additional metadata")

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat(),
            UUID: lambda v: str(v),
        }
