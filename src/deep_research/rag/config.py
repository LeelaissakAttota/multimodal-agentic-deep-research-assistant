"""``MDRA_RAG_*`` settings. Every default keeps RAG off and local."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_LOCAL_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_API_BASE = "https://openrouter.ai/api/v1"
DEFAULT_API_MODEL = "nvidia/nemotron-3-embed-1b:free"


class RagSettings(BaseSettings):
    enabled: bool = Field(default=False, description="turn hybrid RAG on for follow-up chat and the Knowledge Base")
    embeddings: Literal["local", "api"] = Field(default="local", description="local sentence-transformers or an "
                                                "OpenAI-compatible /embeddings endpoint (free models only)")
    model: str = Field(default=DEFAULT_LOCAL_MODEL, min_length=1, max_length=200)
    api_base: str = Field(default=DEFAULT_API_BASE, max_length=300)
    api_model: str = Field(default=DEFAULT_API_MODEL, min_length=1, max_length=200)
    api_key: SecretStr | None = Field(default=None, description="defaults to DEEP_RESEARCH_OPENROUTER_API_KEY")
    index: Literal["auto", "local", "pgvector"] = Field(default="auto", description="auto = pgvector when "
                                                         "MDRA_DATABASE_URL is set and the extension exists, else local")
    index_dir: str = Field(default=str(Path("data") / "rag"))
    chunk_words: int = Field(default=120, ge=20, le=1000)
    overlap_words: int = Field(default=30, ge=0, le=500)
    top_k: int = Field(default=6, ge=1, le=50)
    min_similarity: float = Field(default=0.35, ge=0.0, le=1.0)
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)

    model_config = SettingsConfigDict(
        env_file=os.environ.get("DEEP_RESEARCH_ENV_FILE") or ".env",
        env_file_encoding="utf-8",
        env_prefix="MDRA_RAG_",
        case_sensitive=False,
        extra="ignore",
    )
