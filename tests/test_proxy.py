from typing import Dict, List, Optional, Tuple

import httpx
import pytest
import respx
from starlette.requests import Request

from app.config import Config
from app.models import ApiKey
from app.proxy import proxy_request


class FakeKeyManager:
    def __init__(self, keys: List[ApiKey]):
        self.keys: List[ApiKey] = keys
        self.select_calls: int = 0
        self.requests: List[str] = []
        self.errors: List[Tuple[str, bool]] = []

    async def select_key(self) -> Optional[ApiKey]:
        if self.select_calls >= len(self.keys):
            return None
        key = self.keys[self.select_calls]
        self.select_calls += 1
        return key

    async def record_request(self, key_id: str) -> None:
        self.requests.append(key_id)

    async def record_error(self, key_id: str, is_rpd_limit: bool = False) -> None:
        self.errors.append((key_id, is_rpd_limit))


def make_request(
    method: str = "POST",
    path: str = "/v1beta/models",
    query_string: str = "",
    headers: Optional[Dict[str, str]] = None,
    body: bytes = b"{}",
    path_params: Optional[Dict[str, str]] = None,
):
    headers = headers or {}
    raw_headers: List[Tuple[bytes, bytes]] = [
        (k.lower().encode(), v.encode()) for k, v in headers.items()
    ]
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string.encode(),
        "headers": raw_headers,
        "path_params": path_params or {},
        "client": ("testclient", 123),
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def make_config(max_retries: int = 2, retry_delay_seconds: int = 0) -> Config:
    return Config(
        api_keys=["k1", "k2"],
        max_retries=max_retries,
        retry_delay_seconds=retry_delay_seconds,
        gemini_base_url="https://gemini.example.test",
    )


def make_key(key_id: str, key_value: str) -> ApiKey:
    return ApiKey(id=key_id, key=key_value)


@pytest.mark.asyncio
@respx.mock
async def test_proxy_successful_forward():
    config = make_config()
    key_manager = FakeKeyManager([make_key("k1", "server-key-1")])
    headers = {
        "content-type": "application/json",
        "x-goog-api-key": "caller-key",
        "custom-header": "custom-value",
        "host": "incoming.example",
    }
    request = make_request(
        headers=headers,
        path_params={"path": "v1beta/models"},
    )

    captured: List[httpx.Request] = []
    responses = [httpx.Response(200, json={"ok": True})]

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return responses.pop(0)

    _ = respx.post("https://gemini.example.test/v1beta/models").mock(
        side_effect=handler
    )

    async with httpx.AsyncClient(base_url=config.gemini_base_url) as client:
        response = await proxy_request(request, key_manager, client, config)

    assert response.status_code == 200
    assert len(captured) == 1
    sent = captured[0]
    assert sent.headers["x-goog-api-key"] == "server-key-1"
    assert sent.headers["custom-header"] == "custom-value"
    assert sent.headers["host"] == "gemini.example.test"


@pytest.mark.asyncio
@respx.mock
async def test_proxy_429_retry_switches_key():
    config = make_config(max_retries=3)
    key_manager = FakeKeyManager(
        [make_key("k1", "server-key-1"), make_key("k2", "server-key-2")]
    )
    request = make_request(path_params={"path": "v1beta/models"})

    captured: List[httpx.Request] = []
    responses = [
        httpx.Response(429, json={"error": {"message": "Per day limit"}}),
        httpx.Response(200, json={"ok": True}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return responses.pop(0)

    _ = respx.post("https://gemini.example.test/v1beta/models").mock(
        side_effect=handler
    )

    async with httpx.AsyncClient(base_url=config.gemini_base_url) as client:
        response = await proxy_request(request, key_manager, client, config)

    assert response.status_code == 200
    assert key_manager.errors == [("k1", True)]
    assert captured[0].headers["x-goog-api-key"] == "server-key-1"
    assert captured[1].headers["x-goog-api-key"] == "server-key-2"


@pytest.mark.asyncio
@respx.mock
async def test_proxy_429_rpm_retries_same_key():
    config = make_config(max_retries=2, retry_delay_seconds=0)
    key_manager = FakeKeyManager([make_key("k1", "server-key-1")])
    request = make_request(path_params={"path": "v1beta/models"})

    captured: List[httpx.Request] = []
    responses = [
        httpx.Response(429, json={"error": {"message": "Per minute limit"}}),
        httpx.Response(200, json={"ok": True}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return responses.pop(0)

    _ = respx.post("https://gemini.example.test/v1beta/models").mock(
        side_effect=handler
    )

    async with httpx.AsyncClient(base_url=config.gemini_base_url) as client:
        response = await proxy_request(request, key_manager, client, config)

    assert response.status_code == 200
    assert key_manager.errors == [("k1", False)]
    assert key_manager.select_calls == 1
    assert captured[0].headers["x-goog-api-key"] == "server-key-1"
    assert captured[1].headers["x-goog-api-key"] == "server-key-1"


@pytest.mark.asyncio
@respx.mock
async def test_proxy_all_keys_exhausted_503():
    config = make_config(max_retries=2, retry_delay_seconds=0)
    key_manager = FakeKeyManager(
        [make_key("k1", "server-key-1"), make_key("k2", "server-key-2")]
    )
    request = make_request(path_params={"path": "v1beta/models"})

    responses = [
        httpx.Response(429, json={"error": {"message": "Per day limit"}}),
        httpx.Response(429, json={"error": {"message": "Per day limit"}}),
    ]

    def handler(_: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    _ = respx.post("https://gemini.example.test/v1beta/models").mock(
        side_effect=handler
    )

    async with httpx.AsyncClient(base_url=config.gemini_base_url) as client:
        response = await proxy_request(request, key_manager, client, config)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "60"


@pytest.mark.asyncio
@respx.mock
async def test_proxy_no_keys_available_503():
    config = make_config(max_retries=1, retry_delay_seconds=0)
    key_manager = FakeKeyManager([])
    request = make_request(path_params={"path": "v1beta/models"})

    async with httpx.AsyncClient(base_url=config.gemini_base_url) as client:
        response = await proxy_request(request, key_manager, client, config)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "60"


@pytest.mark.asyncio
@respx.mock
async def test_proxy_strips_hop_by_hop_headers():
    config = make_config()
    key_manager = FakeKeyManager([make_key("k1", "server-key-1")])
    headers = {
        "connection": "keep-alive",
        "proxy-authorization": "secret",
        "upgrade": "websocket",
        "te": "trailers",
        "host": "incoming.example",
    }
    request = make_request(
        headers=headers,
        path_params={"path": "v1beta/models"},
    )

    captured: List[httpx.Request] = []
    responses = [httpx.Response(200, json={"ok": True})]

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return responses.pop(0)

    _ = respx.post("https://gemini.example.test/v1beta/models").mock(
        side_effect=handler
    )

    async with httpx.AsyncClient(base_url=config.gemini_base_url) as client:
        response = await proxy_request(request, key_manager, client, config)

    assert response.status_code == 200

    sent_headers = captured[0].headers
    assert "proxy-authorization" not in sent_headers
    assert "upgrade" not in sent_headers
    assert "te" not in sent_headers
    assert sent_headers["host"] == "gemini.example.test"


@pytest.mark.asyncio
@respx.mock
async def test_proxy_strips_caller_api_key():
    config = make_config()
    key_manager = FakeKeyManager([make_key("k1", "server-key-1")])
    headers = {
        "x-goog-api-key": "caller-key",
        "content-type": "application/json",
    }
    request = make_request(
        headers=headers,
        path_params={"path": "v1beta/models"},
    )

    captured: List[httpx.Request] = []
    responses = [httpx.Response(200, json={"ok": True})]

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return responses.pop(0)

    _ = respx.post("https://gemini.example.test/v1beta/models").mock(
        side_effect=handler
    )

    async with httpx.AsyncClient(base_url=config.gemini_base_url) as client:
        response = await proxy_request(request, key_manager, client, config)

    assert response.status_code == 200

    sent_headers = captured[0].headers
    assert sent_headers["x-goog-api-key"] == "server-key-1"


@pytest.mark.asyncio
@respx.mock
async def test_proxy_strips_key_query_param():
    config = make_config()
    key_manager = FakeKeyManager([make_key("k1", "server-key-1")])
    request = make_request(
        query_string="key=caller&foo=bar",
        path_params={"path": "v1beta/models"},
    )

    captured: List[httpx.Request] = []
    responses = [httpx.Response(200, json={"ok": True})]

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return responses.pop(0)

    _ = respx.post("https://gemini.example.test/v1beta/models").mock(
        side_effect=handler
    )

    async with httpx.AsyncClient(base_url=config.gemini_base_url) as client:
        response = await proxy_request(request, key_manager, client, config)

    assert response.status_code == 200

    sent_url = str(captured[0].url)
    assert "key=" not in sent_url
    assert "foo=bar" in sent_url


@pytest.mark.asyncio
@respx.mock
async def test_proxy_timeout_retries():
    config = make_config(max_retries=2, retry_delay_seconds=0)
    key_manager = FakeKeyManager(
        [make_key("k1", "server-key-1"), make_key("k2", "server-key-2")]
    )
    request = make_request(path_params={"path": "v1beta/models"})

    captured: List[httpx.Request] = []
    responses = [
        httpx.TimeoutException("timeout"),
        httpx.Response(200, json={"ok": True}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    _ = respx.post("https://gemini.example.test/v1beta/models").mock(
        side_effect=handler
    )

    async with httpx.AsyncClient(base_url=config.gemini_base_url) as client:
        response = await proxy_request(request, key_manager, client, config)

    assert response.status_code == 200
    assert captured[0].headers["x-goog-api-key"] == "server-key-1"
    assert captured[1].headers["x-goog-api-key"] == "server-key-2"
