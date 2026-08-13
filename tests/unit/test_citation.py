"""
Unit tests for Citation domain model.
"""
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from deep_research.evidence.citation import Citation


def test_citation_creation():
    """Test that a Citation can be created with required fields."""
    claim_id = uuid4()
    evidence_id = uuid4()
    citation = Citation(
        claim_id=claim_id,
        evidence_id=evidence_id,
        identifier="[1]",
        access_timestamp=datetime.now(UTC),
    )
    assert isinstance(citation.id, UUID)
    assert citation.claim_id == claim_id
    assert citation.evidence_id == evidence_id
    assert citation.identifier == "[1]"
    assert isinstance(citation.access_timestamp, datetime)
    assert citation.source_locator is None
    assert citation.author is None
    assert citation.title is None
    assert citation.publication_name is None
    assert citation.metadata == {}


def test_citation_with_optional_fields():
    """Test that a Citation can be created with optional fields."""
    claim_id = uuid4()
    evidence_id = uuid4()
    source_locator = "https://example.com/source"
    author = "John Doe"
    title = "Example Source"
    publication_name = "Example Journal"
    metadata = {"key": "value"}
    citation = Citation(
        claim_id=claim_id,
        evidence_id=evidence_id,
        identifier="[1]",
        source_locator=source_locator,
        access_timestamp=datetime.now(UTC),
        author=author,
        title=title,
        publication_name=publication_name,
        metadata=metadata,
    )
    assert str(citation.source_locator) == source_locator
    assert citation.author == author
    assert citation.title == title
    assert citation.publication_name == publication_name
    assert citation.metadata == metadata


def test_citation_invalid_identifier():
    """Test that a blank report reference is rejected."""
    claim_id = uuid4()
    evidence_id = uuid4()
    with pytest.raises(ValidationError, match="at least 1 character"):
        Citation(
            claim_id=claim_id,
            evidence_id=evidence_id,
            identifier="",
            access_timestamp=datetime.now(UTC),
        )
