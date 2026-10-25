"""``MDRA_MEDIA_*`` settings. Every default keeps media analysis off and local."""

from __future__ import annotations

import os
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_API_BASE = "http://127.0.0.1:8000/v1"


class MediaSettings(BaseSettings):
    enabled: bool = Field(default=False, description="turn audio/video/YouTube analysis on")
    transcriber: Literal["local", "api"] = Field(default="local", description="local faster-whisper, or an "
                                                 "OpenAI-compatible /audio/transcriptions endpoint (free/local only)")
    whisper_model: str = Field(default="base", min_length=1, max_length=200)
    whisper_device: str = Field(default="auto", max_length=20)
    whisper_compute_type: str = Field(default="int8", max_length=20)
    language: str | None = Field(default=None, max_length=10, description="ISO code; None = auto-detect")
    api_base: str = Field(default=DEFAULT_API_BASE, max_length=300)
    api_model: str = Field(default="whisper-1", min_length=1, max_length=200)
    api_key: SecretStr | None = None
    prefer_captions: bool = Field(default=True, description="YouTube: use existing captions before transcribing")
    keyframes: int = Field(default=6, ge=0, le=60, description="video keyframes to caption (0 = none)")
    youtube_frames: bool = Field(default=False, description="download a low-res YouTube video for keyframes")
    window_seconds: int = Field(default=60, ge=10, le=600, description="transcript seconds per cited chunk")
    max_duration_seconds: int = Field(default=3 * 3600, ge=60, le=24 * 3600)
    max_download_mb: int = Field(default=200, ge=1, le=4000)
    timeout_seconds: float = Field(default=600.0, gt=0, le=7200)

    model_config = SettingsConfigDict(
        env_file=os.environ.get("DEEP_RESEARCH_ENV_FILE") or ".env",
        env_file_encoding="utf-8",
        env_prefix="MDRA_MEDIA_",
        case_sensitive=False,
        extra="ignore",
    )
