from pydantic import Field
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
    version: str = Field(default="0.1.0")

    # API
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000)

    # Logging
    log_level: str = Field(default="INFO")

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

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        env_prefix="MADRA_",
    )

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
