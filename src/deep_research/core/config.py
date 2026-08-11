"""
Application configuration module.
"""
from typing import Optional
from pydantic import BaseSettings, Field


class Settings(BaseSettings):
    """
    Application settings.
    """
    # Application
    app_name: str = Field(default="Multimodal Agentic Deep Research Assistant", env="MADRA_APP_NAME")
    environment: str = Field(default="development", env="MADRA_ENVIRONMENT")
    debug: bool = Field(default=False, env="MADRA_DEBUG")
    version: str = Field(default="0.1.0", env="MADRA_VERSION")

    # API
    api_host: str = Field(default="0.0.0.0", env="MADRA_API_HOST")
    api_port: int = Field(default=8000, env="MADRA_API_PORT")

    # Logging
    log_level: str = Field(default="INFO", env="MADRA_LOG_LEVEL")

    # Future configurable budgets (placeholders for Phase 1)
    max_research_iterations: int = Field(default=3, env="MADRA_MAX_RESEARCH_ITERATIONS")
    max_tool_calls_per_iteration: int = Field(default=20, env="MADRA_MAX_TOOL_CALLS_PER_ITERATION")
    max_model_calls_per_iteration: int = Field(default=15, env="MADRA_MAX_MODEL_CALLS_PER_ITERATION")
    max_research_time_seconds: int = Field(default=300, env="MADRA_MAX_RESEARCH_TIME_SECONDS")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False


# Global settings instance
settings = Settings()
