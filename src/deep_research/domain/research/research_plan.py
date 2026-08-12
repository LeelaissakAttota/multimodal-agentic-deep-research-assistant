"""
Research Plan domain model.
"""
from uuid import UUID, uuid4
from datetime import datetime
from typing import List, Dict, Any
from pydantic import BaseModel, Field

from deep_research.domain.research.research_task import ResearchTask


class ResearchPlan(BaseModel):
    """
    Represents a plan to fulfill a research request.
    """
    id: UUID = Field(default_factory=uuid4, description="Unique identifier for the plan")
    request_id: UUID = Field(..., description="ID of the research request this plan addresses")
    objective: str = Field(..., description="Restated research objective from the request")
    tasks: List[ResearchTask] = Field(default_factory=list, description="Ordered list of research tasks to execute")
    created_at: datetime = Field(default_factory=datetime.utcnow, description="When the plan was created")
    updated_at: datetime = Field(default_factory=datetime.utcnow, description="When the plan was last updated")
    status: str = Field(default="draft", description="Current status of the plan (draft, active, completed, failed)")
    progress: float = Field(default=0.0, ge=0.0, le=1.0, description="Proportion of tasks completed")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata")

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat(),
            UUID: lambda v: str(v),
        }
