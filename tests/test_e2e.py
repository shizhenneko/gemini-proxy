"""End-to-end tests with real Gemini API."""

import os
import pytest
import httpx
from httpx import AsyncClient, ASGITransport
from app.main import app as main_app
from app.config import load_config
from app.key_manager import KeyManager


GEMINI_TEST_KEY = os.getenv("GEMINI_TEST_KEY", "")


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.skipif(not GEMINI_TEST_KEY, reason="GEMINI_TEST_KEY not set")
async def test_e2e_proxy_real_gemini(monkeypatch):
    """Smoke test: proxy a real request to Gemini API."""
    monkeypatch.setenv("GEMINI_API_KEYS", GEMINI_TEST_KEY)

    config = load_config(use_dotenv=False)
    http_client = httpx.AsyncClient(
        base_url=config.gemini_base_url,
        timeout=httpx.Timeout(10.0, read=300.0, write=30.0),
        follow_redirects=True,
    )
    key_manager = KeyManager(config)

    main_app.state.config = config
    main_app.state.http_client = http_client
    main_app.state.key_manager = key_manager

    try:
        async with AsyncClient(
            transport=ASGITransport(app=main_app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1beta/models/gemini-2.5-flash:generateContent",
                json={"contents": [{"parts": [{"text": "Say hello in one word"}]}]},
                timeout=30.0,
            )

            assert response.status_code == 200
            data = response.json()
            assert "candidates" in data
            assert len(data["candidates"]) > 0
            assert "content" in data["candidates"][0]
    finally:
        await http_client.aclose()

        if hasattr(main_app.state, "config"):
            del main_app.state.config
        if hasattr(main_app.state, "http_client"):
            del main_app.state.http_client
        if hasattr(main_app.state, "key_manager"):
            del main_app.state.key_manager
