"""Optional audio/video analysis: transcripts and keyframe captions with timestamps.

Off by default (``MDRA_MEDIA_ENABLED=false``). Install with ``pip install -e ".[media]"``
(faster-whisper, yt-dlp, PyAV); ``ffmpeg`` on PATH is optional (keyframes fall back to PyAV).
"""

from deep_research.media.config import MediaSettings
from deep_research.media.timecode import Segment, fmt_ts

__all__ = ["MediaSettings", "Segment", "fmt_ts"]
