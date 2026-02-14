import asyncio
import logging
from typing import Dict, List, Optional, Protocol, Tuple, cast
from urllib.parse import parse_qsl, urlencode

import httpx
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from app.config import Config
from app.models import ApiKey

logger = logging.getLogger(__name__)

HOP_BY_HOP_HEADERS = frozenset(
    {
        "host",
        "connection",
        "keep-alive",
        "transfer-encoding",
        "te",
        "trailer",
        "upgrade",
        "proxy-authorization",
        "proxy-authenticate",
        "content-encoding",
        "content-length",
    }
)


def _prepare_headers(request_headers: Dict[str, str]) -> Dict[str, str]:
    """Remove hop-by-hop headers and x-goog-api-key from incoming request."""
    return {
        k: v
        for k, v in request_headers.items()
        if k.lower() not in HOP_BY_HOP_HEADERS and k.lower() != "x-goog-api-key"
    }


def _strip_key_from_query(query_string: str) -> str:
    """Remove ?key= parameter from query string if present."""
    if not query_string:
        return ""
    params = [(k, v) for k, v in parse_qsl(query_string, keep_blank_values=True)]
    filtered = [(k, v) for k, v in params if k != "key"]
    return urlencode(filtered, doseq=True)


class KeyManager(Protocol):
    async def select_key(self) -> Optional[ApiKey]: ...

    async def record_request(self, key_id: str) -> None: ...

    async def record_error(self, key_id: str, is_rpd_limit: bool = False) -> None: ...


async def proxy_request(
    request: Request,
    key_manager: KeyManager,
    http_client: httpx.AsyncClient,
    config: Config,
) -> Response:
    """
    Forward request to Gemini API with automatic key selection and 429 retry.

    Flow:
    1. Prepare headers (strip hop-by-hop, strip caller's api key)
    2. Strip ?key= from query params
    3. Loop up to config.max_retries times:
       a. select_key() from key_manager
       b. If no key available -> return 503
       c. Inject x-goog-api-key header
       d. Forward request via httpx
       e. record_request() on key_manager
       f. If response is 429:
          - Parse error to determine if RPD or RPM limit
          - record_error() on key_manager
          - If RPD: mark exhausted, continue loop (try next key)
          - If RPM: wait config.retry_delay_seconds, continue loop (retry same key)
       g. If response is not 429: return response
    4. If all retries exhausted -> return 503 with Retry-After header
    """

    path_param = cast(Optional[str], request.path_params.get("path"))
    path = path_param if path_param is not None else ""

    body = await request.body()
    headers = _prepare_headers(dict(request.headers))
    query_string = _strip_key_from_query(request.url.query)
    query_params: List[Tuple[str, str]] = parse_qsl(
        query_string, keep_blank_values=True
    )
    reuse_key = None

    for attempt in range(config.max_retries):
        selected_key = reuse_key
        reuse_key = None

        if selected_key is None:
            selected_key = await key_manager.select_key()

        if selected_key is None:
            return Response(
                content=(
                    '{"error": {"code": 503, "message": '
                    '"All API keys exhausted", "status": "UNAVAILABLE"}}'
                ),
                status_code=503,
                media_type="application/json",
                headers={"Retry-After": "60"},
            )

        forward_headers = {**headers, "x-goog-api-key": selected_key.key}

        try:
            is_streaming = dict(query_params).get("alt") == "sse"

            if is_streaming:
                return await _handle_streaming_request(
                    config,
                    path,
                    request.method,
                    forward_headers,
                    body,
                    query_params,
                    key_manager,
                    selected_key,
                )

            response = await http_client.request(
                method=request.method,
                url=f"/{path}",
                content=body,
                headers=forward_headers,
                params=tuple(query_params),
            )

            await key_manager.record_request(selected_key.id)

            if response.status_code == 429:
                is_rpd = _is_rpd_limit(response)
                await key_manager.record_error(selected_key.id, is_rpd_limit=is_rpd)
                logger.warning(
                    "429 from Gemini (key=%s, type=%s, attempt=%s)",
                    selected_key.key_prefix(),
                    "RPD" if is_rpd else "RPM",
                    attempt + 1,
                )
                if not is_rpd:
                    await asyncio.sleep(config.retry_delay_seconds)
                    reuse_key = selected_key
                continue

            resp_headers = {
                k: v
                for k, v in response.headers.items()
                if k.lower() not in HOP_BY_HOP_HEADERS
            }
            media_type = cast(Optional[str], response.headers.get("content-type"))
            return Response(
                content=response.content,
                status_code=response.status_code,
                headers=resp_headers,
                media_type=media_type,
            )

        except httpx.TimeoutException:
            logger.error(
                "Timeout forwarding to Gemini (key=%s)", selected_key.key_prefix()
            )
            await key_manager.record_error(selected_key.id)
            continue
        except httpx.RequestError as exc:
            logger.error("Request error: %s", exc)
            await key_manager.record_error(selected_key.id)
            continue

    return Response(
        content=(
            '{"error": {"code": 503, "message": '
            '"Service temporarily unavailable", "status": "UNAVAILABLE"}}'
        ),
        status_code=503,
        media_type="application/json",
        headers={"Retry-After": "60"},
    )


def _is_rpd_limit(response: httpx.Response) -> bool:
    """Determine if a 429 response is due to RPD (daily) or RPM (per-minute) limit."""
    try:
        data = cast(Dict[str, object], response.json())
        error_obj = data.get("error", {})
        if isinstance(error_obj, dict):
            error_dict = cast(Dict[str, object], error_obj)
        else:
            error_dict = {}
        message = error_dict.get("message", "")
        error_message = str(message).lower()
        if "per day" in error_message or "daily" in error_message:
            return True
        return False
    except Exception:
        return False


async def _handle_streaming_request(
    config: Config,
    path: str,
    method: str,
    headers: Dict[str, str],
    body: bytes,
    query_params: List[Tuple[str, str]],
    key_manager: KeyManager,
    selected_key: ApiKey,
) -> StreamingResponse:
    """Handle streaming (SSE) requests."""

    async def stream_generator():
        async with httpx.AsyncClient(
            base_url=config.gemini_base_url,
            timeout=httpx.Timeout(10.0, read=None),
        ) as streaming_client:
            async with streaming_client.stream(
                method=method,
                url=f"/{path}",
                content=body,
                headers=headers,
                params=tuple(query_params),
            ) as response:
                await key_manager.record_request(selected_key.id)

                if response.status_code != 200:
                    error_body = await response.aread()
                    if error_body:
                        yield error_body
                    return

                async for chunk in response.aiter_bytes():
                    if chunk:
                        yield chunk

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
    )
