import os
import pytest
import httpx
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


def test_get_all_status(app):
    client = TestClient(app)
    response = client.get("/admin/status")
    assert response.status_code == 200
    data = response.json()
    assert "total_keys" in data
    assert "available_keys" in data
    assert "keys" in data
    assert isinstance(data["keys"], list)
    assert len(data["keys"]) == 2


def test_get_key_status(app):
    client = TestClient(app)
    response = client.get("/admin/status/key_1")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "key_1"
    assert "key_prefix" in data
    assert "status" in data
    assert "rpd_used" in data
    assert "rpd_limit" in data
    assert "rpd_remaining" in data
    assert "rpm_limit" in data
    assert "rpm_current" in data


def test_get_key_status_not_found(app):
    client = TestClient(app)
    response = client.get("/admin/status/nonexistent")
    assert response.status_code == 404
    data = response.json()
    assert "detail" in data
    assert "nonexistent" in data["detail"]


def test_reset_counters(app):
    client = TestClient(app)
    response = client.post("/admin/reset")
    assert response.status_code == 200
    data = response.json()
    assert data["message"] == "Counters reset successfully"


def test_add_key(app):
    client = TestClient(app)
    response = client.post(
        "/admin/keys",
        json={"api_key": "new_test_key", "rpd_limit": 300, "rpm_limit": 15},
    )
    assert response.status_code == 201
    data = response.json()
    assert "key_id" in data
    assert data["key_id"] == "key_3"


def test_add_key_missing_api_key(app):
    client = TestClient(app)
    response = client.post("/admin/keys", json={})
    assert response.status_code == 400
    data = response.json()
    assert "detail" in data
    assert "api_key is required" in data["detail"]


def test_add_key_duplicate(app):
    client = TestClient(app)
    response = client.post("/admin/keys", json={"api_key": "test_key_1"})
    assert response.status_code == 409
    data = response.json()
    assert "detail" in data
    assert "already exists" in data["detail"]


def test_remove_key(app):
    client = TestClient(app)
    response = client.delete("/admin/keys/key_1")
    assert response.status_code == 204


def test_remove_key_not_found(app):
    client = TestClient(app)
    response = client.delete("/admin/keys/nonexistent")
    assert response.status_code == 404
    data = response.json()
    assert "detail" in data
    assert "nonexistent" in data["detail"]
