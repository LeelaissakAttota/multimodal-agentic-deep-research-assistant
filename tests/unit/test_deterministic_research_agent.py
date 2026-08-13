"""
Tests for the DeterministicResearchAgent.
"""
from uuid import UUID

import pytest
from deep_research.core.agents.deterministic_research_agent import DeterministicResearchAgent
from deep_research.domain.research.research_task import ResearchTask


@pytest.fixture
def agent():
    return DeterministicResearchAgent()


@pytest.mark.asyncio
async def test_agent_selects_web_search_for_web_query(agent):
    """Test that the agent selects web search for a web-related query."""
    task = ResearchTask(
        id=UUID(int=1),
        plan_id=UUID(int=0),
        description="Search the web for information about AI",
        objective="Find latest developments in AI",
        assigned_tool=None,
        tool_input={"query": "AI developments 2024", "limit": 5}
    )

    result = await agent.execute_task(task)

    assert result.success is True
    assert result.tool_name == "web_search"
    assert result.output is not None
    # Check that the output is of the expected type (FakeWebSearchOutput)
    assert hasattr(result.output, 'results')
    assert hasattr(result.output, 'total_results')
    assert isinstance(result.output.results, list)
    assert len(result.output.results) > 0
    assert result.execution_time_ms > 0


@pytest.mark.asyncio
async def test_agent_selects_document_reader_for_document_query(agent):
    """Test that the agent selects document reader for a document-related query."""
    task = ResearchTask(
        id=UUID(int=2),
        plan_id=UUID(int=0),
        description="Read the contents of a PDF file",
        objective="Extract text from document.pdf",
        assigned_tool=None,
        tool_input={"document_path": "/path/to/document.pdf", "extract_text": True}
    )

    result = await agent.execute_task(task)

    assert result.success is True
    assert result.tool_name == "document_reader"
    assert result.output is not None
    # Check that the output is of the expected type (FakeDocumentReaderOutput)
    assert hasattr(result.output, 'text_content')
    assert hasattr(result.output, 'page_count')
    assert hasattr(result.output, 'metadata')
    assert isinstance(result.output.text_content, str)
    assert result.output.page_count == 2
    assert result.execution_time_ms > 0


@pytest.mark.asyncio
async def test_agent_falls_back_to_web_search_when_no_tool_matches(agent):
    """Test that the agent falls back to web search when no tool matches."""
    task = ResearchTask(
        id=UUID(int=3),
        plan_id=UUID(int=0),
        description="Some task that doesn't match any tool",
        objective="Do something unknown",
        assigned_tool=None,
        tool_input={}
    )

    result = await agent.execute_task(task)

    # Should fall back to web search (since it's the default)
    assert result.success is True
    assert result.tool_name == "web_search"


@pytest.mark.asyncio
async def test_agent_handles_assigned_tool(agent):
    """Test that the agent uses the assigned tool if provided."""
    task = ResearchTask(
        id=UUID(int=4),
        plan_id=UUID(int=0),
        description="Use the assigned tool",
        objective="Use web search",
        assigned_tool="web_search",
        tool_input={"query": "test", "limit": 1}
    )

    result = await agent.execute_task(task)

    assert result.success is True
    assert result.tool_name == "web_search"


@pytest.mark.asyncio
async def test_agent_handles_tool_execution_error(agent):
    """Test that the agent handles tool execution errors gracefully."""
    # We'll simulate an error by making the tool raise an exception.
    # However, our fake tools don't raise exceptions. We can test by providing
    # an invalid tool name that doesn't exist in the registry.
    task = ResearchTask(
        id=UUID(int=5),
        plan_id=UUID(int=0),
        description="Use a non-existent tool",
        objective="This should fail",
        assigned_tool="non_existent_tool",
        tool_input={}
    )

    result = await agent.execute_task(task)

    assert result.success is False
    assert result.tool_name == "non_existent_tool"
    assert result.error is not None
    assert "Tool class not found" in result.error


if __name__ == "__main__":
    pytest.main([__file__])
