"""
Evidence domain model.
"""
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    """
    Represents a piece of information extracted from a source that supports a claim.
    """
    id: UUID = Field(default_factory=uuid4, description="Unique identifier for the evidence")
    source_id: UUID = Field(..., description="ID of the source this evidence came from")
    content: str = Field(..., description="The actual evidence content (text, data, etc.)")
    content_type: str = Field(default="text", description="Type of content (e.g., text, table, image)")
    extracted_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When the evidence was extracted",
    )
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata (e.g., confidence, extraction method)")
