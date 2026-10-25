import os
from typing import Literal

from pydantic import field_validator, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from deep_research.runtime.contracts import RuntimeLimits


class Settings(BaseSettings):
    """
    Application settings.
    """
    # Application
    app_name: str = Field(default="Multimodal Agentic Deep Research Assistant")
    environment: str = Field(default="development")
    debug: bool = Field(default=False)
    version: str = Field(default="1.0.0")

    # API
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000)

    # Logging
    log_level: str = Field(default="INFO")
    log_format: Literal["json", "text"] = Field(default="json")

    # Phase 6 runtime limits
    max_research_iterations: int = Field(default=3, ge=1)
    max_tool_calls_per_iteration: int = Field(default=20, ge=1)
    max_tool_calls_total: int = Field(default=60, ge=1)
    max_model_calls_per_iteration: int = Field(default=15, ge=1)
    max_model_calls_total: int = Field(default=45, ge=1)
    max_tokens_per_call: int = Field(default=4_000, ge=1)
    max_tokens_total: int = Field(default=50_000, ge=1)
    max_research_time_seconds: float = Field(default=300.0, gt=0)
    max_tool_call_time_seconds: float = Field(default=30.0, gt=0)
    max_model_call_time_seconds: float = Field(default=60.0, gt=0)
    max_external_api_calls: int = Field(default=10, ge=0)
    max_tool_retry_attempts: int = Field(default=2, ge=0)
    max_model_retry_attempts: int = Field(default=2, ge=0)
    retry_backoff_seconds: float = Field(default=0.1, ge=0)
    retry_backoff_max_seconds: float = Field(default=2.0, ge=0)
    emergency_stop: bool = Field(default=False)

    # Optional live-provider credentials (absent => offline/deterministic).
    openrouter_api_key: SecretStr | None = Field(default=None)
    openrouter_model: str = Field(
        default="auto",
        description="OpenRouter model id ending in :free, or 'auto' to pick from /models at runtime.",
    )
    openrouter_vision_model: str = Field(
        default="auto",
        description="OpenRouter vision model id ending in :free, or 'auto'.",
    )
    openrouter_model_cache_seconds: int = Field(default=21_600, ge=60, le=604_800)
    openrouter_model_cache_path: str | None = Field(
        default=None,
        description="JSON file for the persisted free-model selection. Unset = per-user cache dir; 'off' disables.",
    )
    openrouter_probe_count: int = Field(default=3, ge=0, le=10,
                                        description="Top free models per kind to latency-probe on discovery.")
    openrouter_probe_timeout_seconds: float = Field(default=8.0, gt=0, le=60)
    openrouter_base_url: str = Field(default="https://openrouter.ai/api/v1")
    nvidia_api_key: SecretStr | None = Field(default=None)
    huggingface_api_key: SecretStr | None = Field(default=None)

    # Research mode: "offline" (deterministic, default, no network) or "live"
    research_mode: Literal["offline", "live"] = Field(default="offline")
    search_provider: Literal["auto", "duckduckgo", "tavily"] = Field(default="auto")
    tavily_api_key: SecretStr | None = Field(default=None)
    live_max_search_results: int = Field(default=5, ge=1, le=10)
    live_max_fetch_pages: int = Field(default=2, ge=0, le=5)
    # Keyless public-source tools the planner may route tasks to in live mode:
    # "all" (default), "none", or a comma list of academic_search, youtube_transcript,
    # reddit_search, commons_image_search, hackernews_search, bluesky_search,
    # stackexchange_search.
    live_source_tools: str = Field(default="all", max_length=200)
    # User preferences (set from the assistant profile per run): preferred source tools are always
    # consulted once per plan (comma list); results from blocked domains are dropped.
    preferred_source_tools: str = Field(default="", max_length=200)
    blocked_domains: str = Field(default="", max_length=2000)
    # Read-only share links expire after this many days by default (0 = never); per-share override on create.
    share_ttl_days: float = Field(default=7.0, ge=0, le=365)

    # Agent mode: "auto" = LLM planner/evaluator/tool choice in live mode when an
    # OpenRouter key is set, deterministic otherwise (offline/fixture runs stay
    # stable). "llm" forces the LLM agents in live mode; "deterministic" never uses them.
    # Every LLM decision falls back to the deterministic rule on failure.
    agent_mode: Literal["auto", "llm", "deterministic"] = Field(default="auto")
    agent_llm_timeout_seconds: float = Field(default=40.0, gt=0, le=120)
    agent_max_plan_tasks: int = Field(default=4, ge=1, le=8)
    agent_tool_steps: int = Field(default=3, ge=0, le=6)
    agent_decision_budget_seconds: float = Field(default=40.0, gt=0, le=120)

    # Long-term memory: "auto" = semantic (embeddings + vector store + rerank) when chromadb or
    # sentence-transformers is installed, lexical BoundedResearchMemory otherwise; "off" disables it.
    long_term_memory: Literal["auto", "semantic", "lexical", "off"] = Field(default="auto")
    memory_dir: str = Field(default="data")

    @field_validator("long_term_memory", mode="before")
    @classmethod
    def _normalize_memory_mode(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    model_config = SettingsConfigDict(
        # DEEP_RESEARCH_ENV_FILE lets tests (and ops) point at another file, or a missing one to skip .env.
        env_file=os.environ.get("DEEP_RESEARCH_ENV_FILE") or ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        env_prefix="DEEP_RESEARCH_",
    )

    def enabled_source_tools(self) -> list[str]:
        """Validated list of enabled public-source tool ids (see ``live_source_tools``)."""
        from deep_research.tools.registry import parse_source_tools

        return parse_source_tools(self.live_source_tools)

    def preferred_sources(self) -> list[str]:
        """Preferred source tools that are also enabled (order kept)."""
        from deep_research.tools.registry import parse_source_tools

        enabled = set(self.enabled_source_tools())
        return [t for t in parse_source_tools(self.preferred_source_tools or "none") if t in enabled]

    def blocked_domain_list(self) -> list[str]:
        """Normalized blocked hostnames (scheme, path, port, trailing dot and ``www.`` stripped; lower-case)."""
        from deep_research.security.domains import normalize_domain

        out = [normalize_domain(d) for d in self.blocked_domains.split(",")]
        return [d for d in dict.fromkeys(out) if d]

    def runtime_limits(self) -> RuntimeLimits:
        """Build the validated provider-neutral runtime policy."""
        return RuntimeLimits(
            max_research_iterations=self.max_research_iterations,
            max_tool_calls_per_iteration=self.max_tool_calls_per_iteration,
            max_tool_calls_total=self.max_tool_calls_total,
            max_model_calls_per_iteration=self.max_model_calls_per_iteration,
            max_model_calls_total=self.max_model_calls_total,
            max_tokens_per_call=self.max_tokens_per_call,
            max_tokens_total=self.max_tokens_total,
            max_research_time_seconds=self.max_research_time_seconds,
            max_tool_call_time_seconds=self.max_tool_call_time_seconds,
            max_model_call_time_seconds=self.max_model_call_time_seconds,
            max_external_api_calls=self.max_external_api_calls,
            max_tool_retry_attempts=self.max_tool_retry_attempts,
            max_model_retry_attempts=self.max_model_retry_attempts,
            retry_backoff_seconds=self.retry_backoff_seconds,
            retry_backoff_max_seconds=self.retry_backoff_max_seconds,
            emergency_stop=self.emergency_stop,
        )


# Global settings instance
settings = Settings()
