"""
Source domain model.
"""
from uuid import UUID, uuid4
from datetime import datetime
from typing import Optional, Any
from pydantic import BaseModel, Field, HttpUrl


class Source(BaseModel):
    """
    Represents the origin of information.
    """
    id: UUID = Field(default_factory=uuid4, description="Unique identifier for the source")
    url: Optional[HttpUrl] = Field(default=None, description="URL if the source is a web resource")
    title: Optional[str] = Field(default=None, description="Title or name of the source")
    description: Optional[str] = Field(default=None, description="Description of the source")
    retrieved_at: datetime = Field(default_factory=datetime.utcnow, description="When the source was accessed")
    metadata: dict = Field(default_factory=dict, description="Additional metadata (e.g., author, publication)")

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat(),
            UUID: lambda v: str(v),
        }
