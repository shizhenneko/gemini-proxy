import os
import pytest
from app.config import Config, load_config


def test_config_loads_valid_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEYS", "key1,key2")

    config = load_config(use_dotenv=False)

    assert config.api_keys == ["key1", "key2"]
    assert config.port == 8000
    assert config.host == "0.0.0.0"
    assert config.default_rpd_limit == 250
    assert config.default_rpm_limit == 10
    assert config.max_retries == 3
    assert config.retry_delay_seconds == 2
    assert config.gemini_base_url == "https://generativelanguage.googleapis.com"
    assert config.log_level == "INFO"


def test_config_missing_api_keys(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEYS", raising=False)
    monkeypatch.setattr("app.config.load_dotenv", lambda: None)

    with pytest.raises(
        ValueError,
        match="GEMINI_API_KEYS environment variable must be set and non-empty",
    ):
        load_config(use_dotenv=False)


def test_config_empty_api_keys(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEYS", "")

    with pytest.raises(
        ValueError,
        match="GEMINI_API_KEYS environment variable must be set and non-empty",
    ):
        load_config(use_dotenv=False)


def test_config_custom_values(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEYS", "custom_key")
    monkeypatch.setenv("PORT", "9000")
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("DEFAULT_RPD_LIMIT", "500")
    monkeypatch.setenv("DEFAULT_RPM_LIMIT", "20")
    monkeypatch.setenv("MAX_RETRIES", "5")
    monkeypatch.setenv("RETRY_DELAY_SECONDS", "3")
    monkeypatch.setenv("GEMINI_BASE_URL", "https://custom.api.com")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    config = load_config(use_dotenv=False)

    assert config.api_keys == ["custom_key"]
    assert config.port == 9000
    assert config.host == "127.0.0.1"
    assert config.default_rpd_limit == 500
    assert config.default_rpm_limit == 20
    assert config.max_retries == 5
    assert config.retry_delay_seconds == 3
    assert config.gemini_base_url == "https://custom.api.com"
    assert config.log_level == "DEBUG"


def test_config_strips_whitespace(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEYS", " key1 , key2 ")

    config = load_config(use_dotenv=False)

    assert config.api_keys == ["key1", "key2"]
