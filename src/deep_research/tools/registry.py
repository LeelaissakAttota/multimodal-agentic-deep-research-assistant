"""
Tool registry for managing and discovering research tools.
"""
from typing import Dict, List, Optional, Type
from deep_research.tools.definition import ToolDefinition, ToolCapability
from deep_research.tools.tool import Tool
from deep_research.domain.modality import Modality


class ToolRegistry:
    """Registry for research tools."""

    def __init__(self) -> None:
        self._tools: Dict[str, Type[Tool]] = {}
        self._definitions: Dict[str, ToolDefinition] = {}

    def register(self, tool_class: Type[Tool]) -> None:
        """
        Register a tool class.

        Args:
            tool_class: The tool class to register (must have a no-arg constructor)

        Raises:
            ValueError: If a tool with the same identifier is already registered
        """
        # Instantiate the tool to get its definition
        tool_instance = tool_class()
        definition = tool_instance.get_definition()
        identifier = definition.identifier

        if identifier in self._tools:
            raise ValueError(f"Tool with identifier '{identifier}' is already registered")

        self._tools[identifier] = tool_class
        self._definitions[identifier] = definition

    def get(self, identifier: str) -> Optional[Type[Tool]]:
        """
        Get a tool class by identifier.

        Args:
            identifier: The tool identifier

        Returns:
            The tool class if found, None otherwise
        """
        return self._tools.get(identifier)

    def get_definition(self, identifier: str) -> Optional[ToolDefinition]:
        """
        Get a tool definition by identifier.

        Args:
            identifier: The tool identifier

        Returns:
            The tool definition if found, None otherwise
        """
        return self._definitions.get(identifier)

    def list_tools(self) -> List[str]:
        """
        List all registered tool identifiers.

        Returns:
            List of tool identifiers
        """
        return list(self._tools.keys())

    def list_definitions(self) -> List[ToolDefinition]:
        """
        List all registered tool definitions.

        Returns:
            List of tool definitions
        """
        return list(self._definitions.values())

    def filter_by_modality(self, modality: Modality) -> List[ToolDefinition]:
        """
        Filter tools by modality.

        Args:
            modality: The modality to filter by

        Returns:
            List of tool definitions that support the modality
        """
        return [
            definition for definition in self._definitions.values()
            if definition.modality == modality
        ]

    def filter_by_capability(self, capability: ToolCapability) -> List[ToolDefinition]:
        """
        Filter tools by capability.

        Args:
            capability: The capability to filter by

        Returns:
            List of tool definitions that have the capability
        """
        return [
            definition for definition in self._definitions.values()
            if capability in definition.capabilities
        ]

    def filter_by_modality_and_capability(
        self, modality: Modality, capability: ToolCapability
    ) -> List[ToolDefinition]:
        """
        Filter tools by modality and capability.

        Args:
            modality: The modality to filter by
            capability: The capability to filter by

        Returns:
            List of tool definitions that match both criteria
        """
        return [
            definition for definition in self._definitions.values()
            if definition.modality == modality and capability in definition.capabilities
        ]


# ---------------------------------------------------------------------------
# Phase 2 public-source tools (keyless, live mode only)
# ---------------------------------------------------------------------------

#: Source tool id -> (source type label used in citations, planner hint).
SOURCE_TOOLS: Dict[str, tuple[str, str]] = {
    "academic_search": ("academic", "peer-reviewed papers and preprints (arXiv, Semantic Scholar, OpenAlex)"),
    "youtube_transcript": ("video", "YouTube video transcripts (lectures, talks, explainers)"),
    "reddit_search": ("social", "Reddit community discussion (opinions, first-hand experience; not authoritative)"),
    "commons_image_search": ("image", "freely licensed images/diagrams from Wikimedia Commons"),
}
SOURCE_TOOL_IDS: tuple[str, ...] = tuple(SOURCE_TOOLS)


def parse_source_tools(value: str | None) -> List[str]:
    """``"all"``/empty-default -> every source tool; ``"none"``/``""`` -> none; else a comma list (unknown ids dropped)."""
    text = (value if value is not None else "all").strip().lower()
    if text == "all":
        return list(SOURCE_TOOL_IDS)
    if text in ("", "none", "off"):
        return []
    return [t for t in dict.fromkeys(p.strip() for p in text.split(",")) if t in SOURCE_TOOLS]


def build_source_tools(enabled: List[str] | None = None) -> Dict[str, Tool]:
    """Instantiate the enabled keyless source tools (all by default)."""
    from deep_research.tools.academic_tool import AcademicSearchTool
    from deep_research.tools.commons_image_tool import CommonsImageSearchTool
    from deep_research.tools.reddit_tool import RedditSearchTool
    from deep_research.tools.youtube_tool import YouTubeTranscriptTool

    classes: Dict[str, Type[Tool]] = {
        "academic_search": AcademicSearchTool, "youtube_transcript": YouTubeTranscriptTool,
        "reddit_search": RedditSearchTool, "commons_image_search": CommonsImageSearchTool,
    }
    wanted = list(SOURCE_TOOL_IDS) if enabled is None else enabled
    return {tool_id: classes[tool_id]() for tool_id in wanted if tool_id in classes}


def default_registry(enabled_sources: List[str] | None = None) -> ToolRegistry:
    """Registry with the real multimodal tools plus the enabled public-source tools."""
    from deep_research.tools.image_tool import ImageAnalysisTool
    from deep_research.tools.pdf_tool import PdfReaderTool

    registry = ToolRegistry()
    for cls in (PdfReaderTool, ImageAnalysisTool):
        registry.register(cls)
    for tool in build_source_tools(enabled_sources).values():
        registry.register(type(tool))
    return registry
