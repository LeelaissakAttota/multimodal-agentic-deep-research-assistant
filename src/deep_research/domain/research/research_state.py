"""
Research State domain model.
"""
from uuid import UUID, uuid4
from datetime import datetime
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field



class ResearchState(BaseModel):
    """
    Represents the current state of a research session.
    """
    id: UUID = Field(default_factory=uuid4, description="Unique identifier for the research session")
    request_id: UUID = Field(..., description="ID of the original research request")
    current_plan_id: Optional[UUID] = Field(default=None, description="ID of the currently active research plan")
    plan_history: List[UUID] = Field(default_factory=list, description="List of plan IDs that have been used in this session")
    completed_task_ids: List[UUID] = Field(default_factory=list, description="List of task IDs that have been successfully completed")
    failed_task_ids: List[UUID] = Field(default_factory=list, description="List of task IDs that have failed")
    consulted_sources: List[UUID] = Field(default_factory=list, description="List of source IDs that have been consulted")
    gathered_evidence: List[UUID] = Field(default_factory=list, description="List of evidence IDs that have been gathered")
    generated_claims: List[UUID] = Field(default_factory=list, description="List of claim IDs that have been generated")
    created_at: datetime = Field(default_factory=datetime.utcnow, description="When the research session started")
    updated_at: datetime = Field(default_factory=datetime.utcnow, description="When the state was last updated")
    status: str = Field(
        default="initialized",
        description=(
            "Current status of the research session (initialized, planning, researching, "
            "analyzing, evaluating, reporting, completed, failed)"
        ),
    )
    error: Optional[str] = Field(default=None, description="Error message if the research session failed")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata")

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat(),
            UUID: lambda v: str(v),
        }
