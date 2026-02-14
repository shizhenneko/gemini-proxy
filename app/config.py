"""Configuration management for Gemini Proxy."""

import os
from dataclasses import dataclass
from typing import List
from dotenv import load_dotenv


@dataclass
class Config:
    """Application configuration loaded from environment variables."""

    api_keys: List[str]
    port: int = 8000
    host: str = "0.0.0.0"
    default_rpd_limit: int = 250
    default_rpm_limit: int = 10
    max_retries: int = 3
    retry_delay_seconds: int = 2
    gemini_base_url: str = "https://generativelanguage.googleapis.com"
    log_level: str = "INFO"

    def __post_init__(self):
        if not self.api_keys:
            raise ValueError(
                "GEMINI_API_KEYS environment variable must be set and non-empty"
            )


def load_config() -> Config:
    """Load configuration from environment variables.

    Returns:
        Config: Configured application settings

    Raises:
        ValueError: If required environment variables are missing or invalid
    """
    load_dotenv()

    api_keys_raw = os.getenv("GEMINI_API_KEYS", "")
    api_keys = [key.strip() for key in api_keys_raw.split(",") if key.strip()]

    return Config(
        api_keys=api_keys,
        port=int(os.getenv("PORT", "8000")),
        host=os.getenv("HOST", "0.0.0.0"),
        default_rpd_limit=int(os.getenv("DEFAULT_RPD_LIMIT", "250")),
        default_rpm_limit=int(os.getenv("DEFAULT_RPM_LIMIT", "10")),
        max_retries=int(os.getenv("MAX_RETRIES", "3")),
        retry_delay_seconds=int(os.getenv("RETRY_DELAY_SECONDS", "2")),
        gemini_base_url=os.getenv(
            "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com"
        ),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )
