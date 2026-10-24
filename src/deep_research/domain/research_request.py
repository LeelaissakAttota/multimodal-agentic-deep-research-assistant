"""
Research Request domain model.
"""
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


#: Maps attachment kinds to the tool and parameter that process them.
ATTACHMENT_TOOLS: dict[str, tuple[str, str]] = {
    "pdf": ("pdf_reader", "pdf_data"),
    "image": ("image_analyzer", "image_data"),
    "document": ("document_parser", "document_data"),
}


class ResearchAttachment(BaseModel):
    """A multimodal input embedded as a base64 data URL."""

    kind: Literal["pdf", "image", "document"] = Field(..., description="Attachment type")
    data: str = Field(..., min_length=1, max_length=28_000_000, description="data:<mime>;base64,<payload>")
    name: str | None = Field(default=None, max_length=200, description="Optional display name")


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
    attachments: list[ResearchAttachment] = Field(default_factory=list, description="Optional PDF/image/document inputs")
