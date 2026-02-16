import pytest
import httpx
import respx
from httpx import Response
from fastapi.testclient import TestClient
from app.main import app as main_app
from app.config import load_config
from app.key_manager import KeyManager


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEYS", "test_key_1,test_key_2")


@pytest.fixture
def app():
    config = load_config(use_dotenv=False)
    http_client = httpx.AsyncClient(base_url=config.gemini_base_url)
    key_manager = KeyManager(config)

    main_app.state.config = config
    main_app.state.http_client = http_client
    main_app.state.key_manager = key_manager

    yield main_app

    if hasattr(main_app.state, "config"):
        del main_app.state.config
    if hasattr(main_app.state, "http_client"):
        del main_app.state.http_client
    if hasattr(main_app.state, "key_manager"):
        del main_app.state.key_manager


def test_health_check(app):
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "keys_available" in data
    assert "total_keys" in data
    assert data["total_keys"] == 2


@respx.mock
def test_proxy_route_forwards_request(app):
    gemini_mock = respx.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    ).mock(
        return_value=Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": "Hello from Gemini!"}]}}]
            },
        )
    )

    client = TestClient(app)
    response = client.post(
        "/v1beta/models/gemini-2.5-flash:generateContent",
        json={"contents": [{"parts": [{"text": "Hello"}]}]},
    )
    assert response.status_code == 200
    data = response.json()
    assert "candidates" in data
    assert gemini_mock.called


@respx.mock
def test_proxy_route_injects_api_key(app):
    gemini_mock = respx.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    ).mock(return_value=Response(200, json={"result": "ok"}))

    client = TestClient(app)
    response = client.post(
        "/v1beta/models/gemini-2.5-flash:generateContent",
        json={"contents": [{"parts": [{"text": "Hello"}]}]},
    )
    assert response.status_code == 200

    assert gemini_mock.called
    request = gemini_mock.calls[0].request
    assert "x-goog-api-key" in request.headers
    assert request.headers["x-goog-api-key"] in ["test_key_1", "test_key_2"]
