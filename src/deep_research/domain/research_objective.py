"""
Research Objective value object.
"""
from pydantic import BaseModel, Field


class ResearchObjective(BaseModel):
    """
    The core objective or question driving the research.
    """
    description: str = Field(..., description="Detailed description of the research objective")
    success_criteria: str = Field(default="", description="Criteria for determining when the objective is met")
    context: str = Field(default="", description="Additional context or constraints for the research")
