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
