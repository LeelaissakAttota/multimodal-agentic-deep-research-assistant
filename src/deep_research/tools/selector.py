"""
Tool selector for choosing appropriate tools for research tasks.
"""
from typing import List, Optional, Tuple
from deep_research.tools.registry import ToolRegistry
from deep_research.tools.definition import ToolDefinition, ToolCapability
from deep_research.domain.research.research_task import ResearchTask
from deep_research.domain.modality import Modality
import re


class ToolSelector:
    """Selects appropriate tools for research tasks."""

    def __init__(self, registry: ToolRegistry):
        self.registry = registry

    def select_tool(self, task: ResearchTask) -> Optional[ToolDefinition]:
        """
        Select the best tool for a given research task.

        Args:
            task: The research task to select a tool for

        Returns:
            The selected tool definition, or None if no suitable tool is found
        """
        # If the task has an assigned tool, try to use it
        # If the assigned tool doesn't exist, return None to let the caller handle it
        if task.assigned_tool:
            tool_def = self.registry.get_definition(task.assigned_tool)
            if tool_def:
                return tool_def
            else:
                # Assigned tool doesn't exist - don't fall back to inference
                return None

        # Otherwise, infer the required modality and capability from the task
        required_modality, required_capability, modality_found, capability_found = self._infer_requirements(task)

        # If we found no explicit matches (both modality and capability are defaults because nothing matched),
        # return None to let the agent fall back to its default tool.
        if not modality_found and not capability_found:
            return None

        # Find tools that match the required modality and capability
        candidates = self.registry.filter_by_modality_and_capability(
            required_modality, required_capability
        )

        if not candidates:
            # Fallback: try to find any tool that matches the modality (if we had an explicit modality match)
            if modality_found:
                candidates = self.registry.filter_by_modality(required_modality)

        if not candidates:
            # Fallback: try to find any tool that matches the capability (if we had an explicit capability match)
            if capability_found:
                candidates = self.registry.filter_by_capability(required_capability)

        if not candidates:
            return None

        # For now, return the first candidate (can be extended to rank them)
        return candidates[0]

    def select_tools(self, task: ResearchTask) -> List[ToolDefinition]:
        """
        Select a set of tools for a given research task.

        Args:
            task: The research task to select tools for

        Returns:
            List of tool definitions (minimum useful set)
        """
        # For Phase 3, we start with selecting a single tool.
        # In the future, this could return multiple tools for complex tasks.
        tool = self.select_tool(task)
        return [tool] if tool else []

    def _infer_requirements(self, task: ResearchTask) -> Tuple[Modality, ToolCapability, bool, bool]:
        """
        Infer the required modality and capability from the task description and objective.

        Args:
            task: The research task

        Returns:
            Tuple of (modality, capability, modality_found, capability_found)
            where modality_found and capability_found indicate whether we found an explicit match
            (as opposed to falling back to the default).
        """
        # Combine description and objective for analysis
        text = f"{task.description} {task.objective}".lower()

        # Check for specific modalities and capabilities in the text
        modality_patterns = {
            Modality.WEB: [r'\bweb\b', r'\bsearch\b', r'\bgoogle\b', r'\bwebsite\b', r'\burl\b', r'\bhttp\b'],
            Modality.DOCUMENT: [r'\bdocument\b', r'\bdoc\b', r'\btext\b', r'\bread\b'],
            Modality.PDF: [r'\bpdf\b', r'\bdocument\b'],
            Modality.IMAGE: [r'\bimage\b', r'\bphoto\b', r'\bpicture\b', r'\bvision\b', r'\bocr\b'],
            Modality.VIDEO: [r'\bvideo\b', r'\byoutube\b', r'\btranscript\b'],
            Modality.AUDIO: [r'\baudio\b', r'\bsound\b', r'\btranscribe\b', r'\bspeech\b'],
            Modality.ACADEMIC: [r'\bacademic\b', r'\bresearch\b', r'\bjournal\b', r'\barticle\b', r'\barxiv\b', r'\bscholar\b'],
            Modality.SOCIAL: [r'\bsocial\b', r'\bforum\b', r'\bdiscussion\b', r'\bcommunity\b', r'\breddit\b', r'\btwitter\b'],
            Modality.STRUCTURED_DATA: [r'\bdata\b', r'\bapi\b', r'\bjson\b', r'\bcsv\b', r'\btable\b', r'\bdataset\b']
        }

        capability_patterns = {
            ToolCapability.SEARCH: [r'\bsearch\b', r'\bfind\b', r'\blook\s+up\b'],
            ToolCapability.READ: [r'\bread\b', r'\bextract\b', r'\bget\b'],
            ToolCapability.ANALYZE: [r'\banalyze\b', r'\bexamine\b', r'\bstudy\b'],
            ToolCapability.TRANSCRIBE: [r'\btranscribe\b', r'\btranscript\b'],
            ToolCapability.DESCRIBE: [r'\bdescribe\b', r'\bsummarize\b', r'\bcaption\b'],
            ToolCapability.EXTRACT: [r'\bextract\b', r'\bparse\b', r'\bget\b']
        }

        # Determine modality
        required_modality = Modality.TEXT  # default
        modality_found = False
        for modality, patterns in modality_patterns.items():
            for pattern in patterns:
                if re.search(pattern, text):
                    required_modality = modality
                    modality_found = True
                    break
            if modality_found:
                break

        # Determine capability
        required_capability = ToolCapability.READ  # default
        capability_found = False
        for capability, patterns in capability_patterns.items():
            for pattern in patterns:
                if re.search(pattern, text):
                    required_capability = capability
                    capability_found = True
                    break
            if capability_found:
                break

        return required_modality, required_capability, modality_found, capability_found
