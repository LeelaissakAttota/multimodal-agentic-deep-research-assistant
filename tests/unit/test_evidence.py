"""
Unit tests for evidence models.
"""
from uuid import UUID
from deep_research.evidence.source import Source
from deep_research.evidence.evidence import Evidence
from deep_research.evidence.claim import Claim


def test_source_creation():
    """
    Test that a Source can be created.
    """
    src = Source(title="Test Source", url="https://example.com")
    assert src.title == "Test Source"
    assert str(src.url) == "https://example.com/"
    assert isinstance(src.id, UUID)
    assert src.retrieved_at is not None


def test_evidence_creation():
    """
    Test that an Evidence can be created.
    """
    source_id = UUID('12345678-1234-5678-1234-567812345678')
    ev = Evidence(source_id=source_id, content="Test evidence")
    assert ev.source_id == source_id
    assert ev.content == "Test evidence"
    assert isinstance(ev.id, UUID)
    assert ev.extracted_at is not None


def test_claim_creation():
    """
    Test that a Claim can be created.
    """
    claim = Claim(text="Test claim")
    assert claim.text == "Test claim"
    assert claim.id is not None
    assert claim.confidence == 1.0
    assert claim.supported_by == []
    assert claim.created_at is not None
