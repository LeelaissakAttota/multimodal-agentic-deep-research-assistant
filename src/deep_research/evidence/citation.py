"""
Citation domain model.
"""
from datetime import UTC, datetime
from typing import Any, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, HttpUrl


class Citation(BaseModel):
    """
    Represents a citation linking a claim to its supporting evidence.
    """
    id: UUID = Field(default_factory=uuid4, description="Unique identifier for the citation")
    claim_id: UUID = Field(..., description="ID of the claim this citation supports")
    evidence_id: UUID = Field(..., description="ID of the evidence being cited")
    identifier: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Unique reference within the report (e.g., number)",
    )
    source_locator: Optional[HttpUrl] = Field(
        default=None, description="URL, DOI, file path, or other means to access the source"
    )
    access_timestamp: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When the evidence was collected",
    )
    author: Optional[str] = Field(default=None, description="Author of the source")
    title: Optional[str] = Field(default=None, description="Title of the source")
    publication_name: Optional[str] = Field(
        default=None, description="Publication name (e.g., journal, website)"
    )
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
