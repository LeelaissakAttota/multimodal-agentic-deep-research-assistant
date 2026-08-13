"""
Modality taxonomy for research tools.
"""
from enum import Enum


class Modality(str, Enum):
    """Represents the modality of information that a tool can process or produce."""
    TEXT = "text"
    WEB = "web"
    DOCUMENT = "document"
    PDF = "pdf"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    ACADEMIC = "academic"
    SOCIAL = "social"
    STRUCTURED_DATA = "structured_data"
