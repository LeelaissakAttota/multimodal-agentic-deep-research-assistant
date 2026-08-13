"""Security helpers shared by application and infrastructure boundaries."""

from deep_research.security.data_safety import find_sensitive_data_path

__all__ = ["find_sensitive_data_path"]
