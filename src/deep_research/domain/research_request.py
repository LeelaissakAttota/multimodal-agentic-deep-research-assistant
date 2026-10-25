"""
Research Request domain model.
"""
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator


#: Maps attachment kinds to the tool and parameter that process them.
ATTACHMENT_TOOLS: dict[str, tuple[str, str]] = {
    "pdf": ("pdf_reader", "pdf_data"),
    "image": ("image_analyzer", "image_data"),
    "document": ("document_parser", "document_data"),
    "url": ("web_page_reader", "page_url"),
    "media": ("media_analyzer", "media_data"),
}


class ResearchAttachment(BaseModel):
    """A multimodal input embedded as a base64 data URL."""

    kind: Literal["pdf", "image", "document", "url", "media"] = Field(..., description="Attachment type")
    data: str = Field(..., min_length=1, max_length=28_000_000,
                      description="data:<mime>;base64,<payload>, or an http(s) page URL when kind is 'url'; "
                      "'media' (audio/video) takes either")
    name: str | None = Field(default=None, max_length=200, description="Optional display name")

    @model_validator(mode="after")
    def _check_data(self) -> "ResearchAttachment":
        is_link = self.data.lower().startswith(("http://", "https://"))
        if self.kind == "media" and is_link:
            if len(self.data) > 2_000 or any(c.isspace() for c in self.data):
                raise ValueError("a 'media' link must be one http(s) URL (max 2000 chars)")
        elif self.kind == "url":
            if not self.data.lower().startswith(("http://", "https://")) or len(self.data) > 2_000 or any(
                    c.isspace() for c in self.data):
                raise ValueError("a 'url' attachment needs one http(s) link (max 2000 chars)")
        elif not self.data.startswith("data:"):
            raise ValueError("file attachments must be data: URLs")
        return self


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
