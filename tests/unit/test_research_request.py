"""
Unit tests for ResearchRequest model.
"""
from uuid import UUID
from deep_research.domain.research_request import ResearchRequest


def test_research_request_creation():
    """
    Test that a ResearchRequest can be created and has the expected fields.
    """
    obj = "Test research objective"
    req = ResearchRequest(objective=obj)
    assert req.objective == obj
    assert isinstance(req.id, UUID)
    # Check that the created_at is set (we can't check the exact value, but we can check it's not None)
    assert req.created_at is not None
    assert req.metadata == {}


def test_research_request_with_metadata():
    """
    Test that a ResearchRequest can be created with metadata.
    """
    obj = "Test research objective"
    meta = {"key": "value"}
    req = ResearchRequest(objective=obj, metadata=meta)
    assert req.objective == obj
    assert req.metadata == meta
