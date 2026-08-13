"""
Research Task domain model.
"""
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

class ResearchTask(BaseModel):
    """
    Represents a single task within a research plan.
    """
    id: UUID = Field(default_factory=uuid4, description="Unique identifier for the task")
    plan_id: UUID = Field(..., description="ID of the research plan this task belongs to")
    description: str = Field(..., description="Description of what the task should accomplish")
    objective: str = Field(..., description="Specific objective for this task")
    assigned_tool: Optional[str] = Field(default=None, description="Tool recommended for this task")
    tool_input: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Input parameters for the tool")
    sources_consulted: List[UUID] = Field(default_factory=list, description="List of source IDs consulted during this task")
    evidence_gathered: List[UUID] = Field(default_factory=list, description="List of evidence IDs gathered from this task")
    status: str = Field(default="pending", description="Current status of the task (pending, in_progress, completed, failed)")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When the task was created",
    )
    started_at: Optional[datetime] = Field(default=None, description="When the task started execution")
    completed_at: Optional[datetime] = Field(default=None, description="When the task completed")
    result: Optional[str] = Field(default=None, description="Brief summary of the task result")
    error: Optional[str] = Field(default=None, description="Error message if the task failed")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
