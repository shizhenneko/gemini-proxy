"""Integration tests for Gemini proxy - full stack testing with mocked Gemini API."""

import asyncio
import pytest
import httpx
import respx
from httpx import AsyncClient, ASGITransport, Response
from app.main import app as main_app
from app.config import load_config
from app.key_manager import KeyManager


@pytest.fixture
async def app(monkeypatch):
    """Set up app with test configuration."""
    monkeypatch.setenv("GEMINI_API_KEYS", "test_key_1,test_key_2,test_key_3")

    config = load_config(use_dotenv=False)
    http_client = httpx.AsyncClient(base_url=config.gemini_base_url)
    key_manager = KeyManager(config)

    main_app.state.config = config
    main_app.state.http_client = http_client
    main_app.state.key_manager = key_manager

    yield main_app

    await http_client.aclose()

    if hasattr(main_app.state, "config"):
        del main_app.state.config
    if hasattr(main_app.state, "http_client"):
        del main_app.state.http_client
    if hasattr(main_app.state, "key_manager"):
        del main_app.state.key_manager


@pytest.mark.asyncio
async def test_complete_proxy_flow(app):
    """Test complete proxy flow: request -> key selection -> Gemini (mocked) -> response."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        with respx.mock:
            gemini_mock = respx.post(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent"
            ).mock(
                return_value=Response(
                    200,
                    json={"candidates": [{"content": {"parts": [{"text": "Hello!"}]}}]},
                    headers={"content-type": "application/json"},
                )
            )

            response = await client.post(
                "/v1beta/models/gemini-pro:generateContent",
                json={"contents": [{"parts": [{"text": "Hello"}]}]},
            )

            assert response.status_code == 200
            data = response.json()
            assert "candidates" in data
            assert data["candidates"][0]["content"]["parts"][0]["text"] == "Hello!"
            assert gemini_mock.called

            # Verify key was injected
            request = gemini_mock.calls[0].request
            assert "x-goog-api-key" in request.headers
            assert request.headers["x-goog-api-key"] in [
                "test_key_1",
                "test_key_2",
                "test_key_3",
            ]


@pytest.mark.asyncio
async def test_429_retry_switches_keys(app):
    """Test that 429 RPD error triggers key switch and retry."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        with respx.mock:
            # First call returns 429 with RPD error
            first_mock = respx.post(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent"
            ).mock(
                side_effect=[
                    Response(
                        429,
                        json={
                            "error": {
                                "code": 429,
                                "message": "Resource exhausted: quota exceeded per day",
                            }
                        },
                    ),
                    Response(
                        200,
                        json={
                            "candidates": [
                                {"content": {"parts": [{"text": "Success!"}]}}
                            ]
                        },
                    ),
                ]
            )

            response = await client.post(
                "/v1beta/models/gemini-pro:generateContent",
                json={"contents": [{"parts": [{"text": "Test"}]}]},
            )

            assert response.status_code == 200
            assert first_mock.call_count == 2

            # Verify different keys were used
            first_key = first_mock.calls[0].request.headers["x-goog-api-key"]
            second_key = first_mock.calls[1].request.headers["x-goog-api-key"]
            assert first_key != second_key


@pytest.mark.asyncio
async def test_rpd_exhaustion_marks_key(app):
    """Test that reaching RPD limit marks key as exhausted."""
    key_manager = app.state.key_manager

    for key_id in ["key_2", "key_3"]:
        key = key_manager.pool.keys[key_id]
        key.rpd_used = key.rpd_limit
        key.status = "exhausted"

    key_manager.pool.keys["key_1"].rpd_limit = 2

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        with respx.mock:
            respx.post(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent"
            ).mock(
                return_value=Response(
                    200,
                    json={"candidates": [{"content": {"parts": [{"text": "OK"}]}}]},
                )
            )

            for _ in range(2):
                response = await client.post(
                    "/v1beta/models/gemini-pro:generateContent",
                    json={"contents": [{"parts": [{"text": "Test"}]}]},
                )
                assert response.status_code == 200

            status = await key_manager.get_status()
            key_1_status = next(k for k in status["keys"] if k["id"] == "key_1")
            assert key_1_status["status"] == "exhausted"
            assert key_1_status["rpd_used"] == 2

            response = await client.post(
                "/v1beta/models/gemini-pro:generateContent",
                json={"contents": [{"parts": [{"text": "Test"}]}]},
            )
            assert response.status_code == 503


@pytest.mark.asyncio
async def test_rpm_limit_enforcement(app):
    """Test that RPM limit is enforced (key skipped when at limit)."""
    key_manager = app.state.key_manager

    # Set low RPM limit for testing
    for key in key_manager.pool.keys.values():
        key.rpm_limit = 2

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        with respx.mock:
            mock_route = respx.post(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent"
            ).mock(
                return_value=Response(
                    200,
                    json={"candidates": [{"content": {"parts": [{"text": "OK"}]}}]},
                )
            )

            # Send 5 requests rapidly
            for _ in range(5):
                response = await client.post(
                    "/v1beta/models/gemini-pro:generateContent",
                    json={"contents": [{"parts": [{"text": "Test"}]}]},
                )
                assert response.status_code == 200

            # Verify multiple keys were used (due to RPM limits)
            used_keys = set()
            for call in mock_route.calls:
                used_keys.add(call.request.headers["x-goog-api-key"])

            # With 3 keys and RPM limit of 2, we should use at least 2 keys
            assert len(used_keys) >= 2


@pytest.mark.asyncio
async def test_admin_status_reflects_usage(app):
    """Test that admin status endpoint reflects proxy request usage."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        with respx.mock:
            respx.post(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent"
            ).mock(
                return_value=Response(
                    200,
                    json={"candidates": [{"content": {"parts": [{"text": "OK"}]}}]},
                )
            )

            # Get initial status
            status_response = await client.get("/admin/status")
            assert status_response.status_code == 200
            initial_status = status_response.json()
            initial_total_used = sum(k["rpd_used"] for k in initial_status["keys"])

            # Send 3 proxy requests
            for _ in range(3):
                response = await client.post(
                    "/v1beta/models/gemini-pro:generateContent",
                    json={"contents": [{"parts": [{"text": "Test"}]}]},
                )
                assert response.status_code == 200

            # Check status again
            status_response = await client.get("/admin/status")
            assert status_response.status_code == 200
            final_status = status_response.json()
            final_total_used = sum(k["rpd_used"] for k in final_status["keys"])

            # Total usage should have increased by 3
            assert final_total_used == initial_total_used + 3


@pytest.mark.asyncio
async def test_admin_add_then_use_key(app):
    """Test adding a key via admin API and then using it for proxy requests."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        with respx.mock:
            respx.post(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent"
            ).mock(
                return_value=Response(
                    200,
                    json={"candidates": [{"content": {"parts": [{"text": "OK"}]}}]},
                )
            )

            # Add a new key
            add_response = await client.post(
                "/admin/keys",
                json={"api_key": "new_test_key_123", "rpd_limit": 300, "rpm_limit": 15},
            )
            assert add_response.status_code == 201
            data = add_response.json()
            new_key_id = data["key_id"]
            assert new_key_id == "key_4"

            # Exhaust existing keys to force use of new key
            key_manager = app.state.key_manager
            for key_id in ["key_1", "key_2", "key_3"]:
                key = key_manager.pool.keys[key_id]
                key.rpd_used = key.rpd_limit
                key.status = "exhausted"

            # Send proxy request - should use new key
            response = await client.post(
                "/v1beta/models/gemini-pro:generateContent",
                json={"contents": [{"parts": [{"text": "Test"}]}]},
            )
            assert response.status_code == 200

            # Verify new key was used
            status_response = await client.get(f"/admin/status/{new_key_id}")
            assert status_response.status_code == 200
            key_status = status_response.json()
            assert key_status["rpd_used"] == 1


@pytest.mark.asyncio
async def test_admin_remove_key(app):
    """Test removing a key via admin API and verifying it's no longer used."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Remove key_1
        remove_response = await client.delete("/admin/keys/key_1")
        assert remove_response.status_code == 204

        # Verify key is gone
        status_response = await client.get("/admin/status/key_1")
        assert status_response.status_code == 404

        # Verify total keys decreased
        all_status = await client.get("/admin/status")
        assert all_status.status_code == 200
        data = all_status.json()
        assert data["total_keys"] == 2


@pytest.mark.asyncio
async def test_concurrent_requests_no_race(app):
    """Test that concurrent requests don't cause race conditions in usage tracking."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        with respx.mock:
            respx.post(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent"
            ).mock(
                return_value=Response(
                    200,
                    json={"candidates": [{"content": {"parts": [{"text": "OK"}]}}]},
                )
            )

            # Get initial total usage
            status_response = await client.get("/admin/status")
            initial_status = status_response.json()
            initial_total_used = sum(k["rpd_used"] for k in initial_status["keys"])

            # Send 20 concurrent requests
            tasks = []
            for _ in range(20):
                task = client.post(
                    "/v1beta/models/gemini-pro:generateContent",
                    json={"contents": [{"parts": [{"text": "Test"}]}]},
                )
                tasks.append(task)

            responses = await asyncio.gather(*tasks)

            # All should succeed
            for response in responses:
                assert response.status_code == 200

            # Check final usage
            status_response = await client.get("/admin/status")
            final_status = status_response.json()
            final_total_used = sum(k["rpd_used"] for k in final_status["keys"])

            # Total usage should have increased by exactly 20
            assert final_total_used == initial_total_used + 20
