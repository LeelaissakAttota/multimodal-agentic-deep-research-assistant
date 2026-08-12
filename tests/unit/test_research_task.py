"""
Unit tests for ResearchTask model.
"""
from uuid import UUID
from deep_research.domain.research.research_task import ResearchTask


def test_research_task_creation():
    """
    Test that a ResearchTask can be created and has the expected fields.
    """
    task = ResearchTask(
        plan_id=UUID('12345678-1234-5678-1234-567812345678'),
        description="Test task description",
        objective="Test task objective"
    )
    assert task.plan_id == UUID('12345678-1234-5678-1234-567812345678')
    assert task.description == "Test task description"
    assert task.objective == "Test task objective"
    assert isinstance(task.id, UUID)
    assert task.status == "pending"
    assert task.created_at is not None
    assert task.sources_consulted == []
    assert task.evidence_gathered == []
    assert task.tool_input == {}
    assert task.assigned_tool is None
    assert task.started_at is None
    assert task.completed_at is None
    assert task.result is None
    assert task.error is None
    assert task.metadata == {}


def test_research_task_with_optional_fields():
    """
    Test that a ResearchTask can be created with optional fields.
    """
    task = ResearchTask(
        plan_id=UUID('12345678-1234-5678-1234-567812345678'),
        description="Test task description",
        objective="Test task objective",
        assigned_tool="web_search",
        tool_input={"query": "test"},
        sources_consulted=[UUID('87654321-4321-8765-4321-098765432109')],
        evidence_gathered=[UUID('11111111-1111-1111-1111-111111111111')],
        status="in_progress",
        result="Task completed successfully",
        metadata={"priority": "high"}
    )
    assert task.assigned_tool == "web_search"
    assert task.tool_input == {"query": "test"}
    assert len(task.sources_consulted) == 1
    assert len(task.evidence_gathered) == 1
    assert task.status == "in_progress"
    assert task.result == "Task completed successfully"
    assert task.metadata == {"priority": "high"}
