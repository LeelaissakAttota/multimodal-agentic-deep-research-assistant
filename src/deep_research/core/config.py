"""
Application configuration module.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


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

    # Future configurable budgets (placeholders for Phase 1)
    max_research_iterations: int = Field(default=3)
    max_tool_calls_per_iteration: int = Field(default=20)
    max_model_calls_per_iteration: int = Field(default=15)
    max_research_time_seconds: int = Field(default=300)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        env_prefix="MADRA_",
    )


# Global settings instance
settings = Settings()
