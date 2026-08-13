"""
Research Request domain model.
"""
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class ResearchRequest(BaseModel):
    """
    Represents a user's research question or request.
    """
    id: UUID = Field(default_factory=uuid4, description="Unique identifier for the request")
    objective: str = Field(..., description="The research objective or question")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Timestamp when the request was created",
    )
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata for the request")
