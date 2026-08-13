"""
Unit tests for configuration.
"""
import sys



def test_settings_loaded():
    """
    Test that the settings are loaded and have expected defaults.
    """
    # Ensure a clean module state
    if "deep_research.core.config" in sys.modules:
        del sys.modules["deep_research.core.config"]
    from deep_research.core.config import settings
    assert settings.app_name == "Multimodal Agentic Deep Research Assistant"
    assert settings.version == "0.1.0"
    assert settings.environment == "development"
    assert settings.debug is False
    assert settings.api_host == "0.0.0.0"
    assert settings.api_port == 8000
    assert settings.log_level == "INFO"
    assert settings.max_research_iterations == 3
    assert settings.max_tool_calls_per_iteration == 20
    assert settings.max_tool_calls_total == 60
    assert settings.max_model_calls_per_iteration == 15
    assert settings.max_model_calls_total == 45
    assert settings.max_tokens_per_call == 4000
    assert settings.max_tokens_total == 50000
    assert settings.max_research_time_seconds == 300
    assert settings.max_tool_call_time_seconds == 30
    assert settings.max_external_api_calls == 10
    assert settings.max_tool_retry_attempts == 2
    assert settings.max_model_retry_attempts == 2
    assert settings.emergency_stop is False


def test_settings_from_env(monkeypatch):
    """
    Test that settings can be overridden by environment variables.
    """
    monkeypatch.setenv("MADRA_APP_NAME", "Test App")
    monkeypatch.setenv("MADRA_VERSION", "1.0.0")
    monkeypatch.setenv("MADRA_ENVIRONMENT", "testing")
    monkeypatch.setenv("MADRA_DEBUG", "true")
    monkeypatch.setenv("MADRA_API_HOST", "127.0.0.1")
    monkeypatch.setenv("MADRA_API_PORT", "9000")
    monkeypatch.setenv("MADRA_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("MADRA_MAX_RESEARCH_ITERATIONS", "5")
    monkeypatch.setenv("MADRA_MAX_TOOL_CALLS_PER_ITERATION", "10")
    monkeypatch.setenv("MADRA_MAX_TOOL_CALLS_TOTAL", "20")
    monkeypatch.setenv("MADRA_MAX_MODEL_CALLS_PER_ITERATION", "5")
    monkeypatch.setenv("MADRA_MAX_MODEL_CALLS_TOTAL", "10")
    monkeypatch.setenv("MADRA_MAX_TOKENS_PER_CALL", "100")
    monkeypatch.setenv("MADRA_MAX_TOKENS_TOTAL", "1000")
    monkeypatch.setenv("MADRA_MAX_RESEARCH_TIME_SECONDS", "600")
    monkeypatch.setenv("MADRA_MAX_TOOL_CALL_TIME_SECONDS", "12")
    monkeypatch.setenv("MADRA_MAX_EXTERNAL_API_CALLS", "3")
    monkeypatch.setenv("MADRA_MAX_TOOL_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("MADRA_MAX_MODEL_RETRY_ATTEMPTS", "0")
    monkeypatch.setenv("MADRA_EMERGENCY_STOP", "true")

    # Reload the module to pick up the new environment variables
    if "deep_research.core.config" in sys.modules:
        del sys.modules["deep_research.core.config"]
    from deep_research.core.config import Settings
    test_settings = Settings()
    assert test_settings.app_name == "Test App"
    assert test_settings.version == "1.0.0"
    assert test_settings.environment == "testing"
    assert test_settings.debug is True
    assert test_settings.api_host == "127.0.0.1"
    assert test_settings.api_port == 9000
    assert test_settings.log_level == "DEBUG"
    assert test_settings.max_research_iterations == 5
    assert test_settings.max_tool_calls_per_iteration == 10
    assert test_settings.max_tool_calls_total == 20
    assert test_settings.max_model_calls_per_iteration == 5
    assert test_settings.max_model_calls_total == 10
    assert test_settings.max_tokens_per_call == 100
    assert test_settings.max_tokens_total == 1000
    assert test_settings.max_research_time_seconds == 600
    assert test_settings.max_tool_call_time_seconds == 12
    assert test_settings.max_external_api_calls == 3
    assert test_settings.max_tool_retry_attempts == 1
    assert test_settings.max_model_retry_attempts == 0
    assert test_settings.emergency_stop is True
