"""
Evidence domain model.
"""
from uuid import UUID, uuid4
from datetime import datetime
from typing import Optional, Any, List
from pydantic import BaseModel, Field


class Evidence(BaseModel):
    """
    Represents a piece of information extracted from a source that supports a claim.
    """
    id: UUID = Field(default_factory=uuid4, description="Unique identifier for the evidence")
    source_id: UUID = Field(..., description="ID of the source this evidence came from")
    content: str = Field(..., description="The actual evidence content (text, data, etc.)")
    content_type: str = Field(default="text", description="Type of content (e.g., text, table, image)")
    extracted_at: datetime = Field(default_factory=datetime.utcnow, description="When the evidence was extracted")
    metadata: dict = Field(default_factory=dict, description="Additional metadata (e.g., confidence, extraction method)")

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat(),
            UUID: lambda v: str(v),
        }
