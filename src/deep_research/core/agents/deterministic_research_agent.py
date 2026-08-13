"""
Deterministic implementation of ResearchAgent for testing.
Uses fake tools that return deterministic results for testing.
"""
import asyncio
from typing import Dict, Any, Optional, List
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field

from deep_research.core.agents.research_agent import ResearchAgent
from deep_research.context.context_builder import AgentContext
from deep_research.domain.research.research_task import ResearchTask
from deep_research.tools.tool import Tool, ToolResult, ToolRequest
from deep_research.tools.registry import ToolRegistry
from deep_research.tools.selector import ToolSelector
from deep_research.tools.definition import ToolDefinition, ToolCapability, ToolInput, ToolOutput
from deep_research.domain.modality import Modality


class FakeWebSearchInput(ToolInput):
    query: str
    limit: Optional[int] = 10


class FakeWebSearchOutput(ToolOutput):
    results: List[Dict[str, Any]] = Field(default_factory=list)
    total_results: int = 0
    summary: str = ""
    evidence_ids: list[UUID] = Field(default_factory=list)
    source_ids: list[UUID] = Field(default_factory=list)


class FakeWebSearchTool(Tool):
    """Fake web search tool for testing."""

    def __init__(self) -> None:
        self._definition = ToolDefinition(
            identifier="web_search",
            name="Web Search",
            description="Search the web for information",
            modality=Modality.WEB,
            capabilities=[ToolCapability.SEARCH],
            input_schema=FakeWebSearchInput,
            output_schema=FakeWebSearchOutput,
            cost_class="FREE",
            network_required=True,
            auth_required=False,
            reliability="MEDIUM"
        )

    async def execute(self, request: ToolRequest) -> ToolResult:
        """Execute fake web search."""
        # Simulate some delay
        await asyncio.sleep(0.01)

        # Extract parameters
        params = request.parameters
        query = params.get("query", "")
        limit = params.get("limit", 10)

        # Return fake results
        results = []
        for i in range(min(limit, 3)):  # Return up to 3 fake results
            results.append({
                "title": f"Result {i+1} for '{query}'",
                "url": f"https://example.com/result/{i+1}",
                "snippet": f"This is a fake search result for query: {query}",
                "source": "example.com"
            })

        output = FakeWebSearchOutput(
            results=results,
            total_results=len(results),
            summary=f"Found {len(results)} deterministic results for: {query}",
            evidence_ids=[
                uuid5(NAMESPACE_URL, f"evidence:{item['url']}:{query}")
                for item in results
            ],
            source_ids=[
                uuid5(NAMESPACE_URL, f"source:{item['url']}") for item in results
            ],
        )

        return ToolResult(
            success=True,
            output=output,
            tool_name="web_search",
            execution_time_ms=10.0,
            metadata={"query": query, "limit": limit}
        )

    async def health_check(self) -> bool:
        """Check if the tool is healthy."""
        return True

    @property
    def name(self) -> str:
        return "web_search"

    def get_definition(self) -> ToolDefinition:
        return self._definition


class FakeDocumentReaderInput(ToolInput):
    document_path: str
    extract_text: bool = True


class FakeDocumentReaderOutput(ToolOutput):
    text_content: str = ""
    page_count: int = 0
    metadata: Dict[str, Any] = Field(default_factory=dict)


class FakeDocumentReaderTool(Tool):
    """Fake document reader tool for testing."""

    def __init__(self) -> None:
        self._definition = ToolDefinition(
            identifier="document_reader",
            name="Document Reader",
            description="Read and extract text from documents",
            modality=Modality.DOCUMENT,
            capabilities=[ToolCapability.READ, ToolCapability.EXTRACT],
            input_schema=FakeDocumentReaderInput,
            output_schema=FakeDocumentReaderOutput,
            cost_class="FREE",
            network_required=False,
            auth_required=False,
            reliability="HIGH"
        )

    async def execute(self, request: ToolRequest) -> ToolResult:
        """Execute fake document reader."""
        # Simulate some delay
        await asyncio.sleep(0.01)

        # Extract parameters
        params = request.parameters
        document_path = params.get("document_path", "")
        extract_text = params.get("extract_text", True)

        # Return fake content
        text_content = f"This is fake extracted text from document: {document_path}\n" \
                      f"It contains some sample content for testing purposes.\n" \
                      f"Document path: {document_path}"

        output = FakeDocumentReaderOutput(
            text_content=text_content,
            page_count=2,
            metadata={"document_path": document_path, "extract_text": extract_text}
        )

        return ToolResult(
            success=True,
            output=output,
            tool_name="document_reader",
            execution_time_ms=15.0,
            metadata={"document_path": document_path}
        )

    async def health_check(self) -> bool:
        """Check if the tool is healthy."""
        return True

    @property
    def name(self) -> str:
        return "document_reader"

    def get_definition(self) -> ToolDefinition:
        return self._definition


class DeterministicResearchAgent(ResearchAgent):
    """
    Deterministic implementation of ResearchAgent for testing.
    Uses the tool registry and selector to choose and execute tools.
    """

    def __init__(self) -> None:
        self.registry = ToolRegistry()
        self.selector = ToolSelector(self.registry)
        self._tools: Dict[str, Any] = {}
        self._register_default_tools()

    def _register_default_tools(self) -> None:
        """Register default fake tools for testing."""
        # Register web search tool
        web_search_tool = FakeWebSearchTool()
        self.registry.register(type(web_search_tool))
        self._tools["web_search"] = web_search_tool

        # Register document reader tool
        document_reader_tool = FakeDocumentReaderTool()
        self.registry.register(type(document_reader_tool))
        self._tools["document_reader"] = document_reader_tool

    async def execute_task(
        self, task: ResearchTask, context: AgentContext | None = None
    ) -> ToolResult:
        """
        Execute a research task using an appropriate tool selected based on the task.

        Args:
            task: The research task to execute

        Returns:
            The result of the tool execution
        """
        # If a tool is explicitly assigned, use it (don't fall back if it doesn't exist)
        if task.assigned_tool:
            tool_definition = self.registry.get_definition(task.assigned_tool)
            if not tool_definition:
                # Assigned tool doesn't exist - return error
                return ToolResult(
                    success=False,
                    error=f"Tool class not found for identifier: {task.assigned_tool}",
                    tool_name=task.assigned_tool,
                    execution_time_ms=0.0
                )
        else:
            # No tool assigned - try to select one based on the task
            tool_definition = self.selector.select_tool(task)

            if not tool_definition:
                # Fallback to a default tool if none found
                tool_definition = self.registry.get_definition("web_search")
                if not tool_definition:
                    # Last resort: create a generic tool result
                    return ToolResult(
                        success=False,
                        error=f"No suitable tool found for task: {task.objective}",
                        tool_name="unknown",
                        execution_time_ms=0.0
                    )

        # Get the tool instance
        tool_class = self.registry.get(tool_definition.identifier)
        if not tool_class:
            return ToolResult(
                success=False,
                error=f"Tool class not found for identifier: {tool_definition.identifier}",
                tool_name=tool_definition.identifier,
                execution_time_ms=0.0
            )

        # Instantiate the tool (in a real implementation, we might cache instances)
        tool_instance = tool_class()

        # Create tool request from task
        tool_request = ToolRequest(
            tool_name=tool_definition.identifier,
            parameters=task.tool_input or {}
        )

        # Execute the tool
        try:
            result = await tool_instance.execute(tool_request)
            return result
        except Exception as exc:
            failure_kind = (
                "timeout"
                if isinstance(exc, asyncio.TimeoutError)
                else "transient" if isinstance(exc, ConnectionError) else "permanent"
            )
            return ToolResult(
                success=False,
                error="Tool execution failed",
                tool_name=tool_definition.identifier,
                execution_time_ms=0.0,
                metadata={
                    "exception_type": type(exc).__name__,
                    "failure_kind": failure_kind,
                    "retryable": failure_kind in {"timeout", "transient"},
                }
            )
