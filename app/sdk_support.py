"""SDK support endpoints for clients that can't use HTTP proxy (e.g. google-generativeai SDK).

Provides key allocation/release API so SDK clients can borrow real API keys
from the pool, use them directly with the SDK, and report usage back.
"""

from typing import Dict

from fastapi import APIRouter, Request, HTTPException
from starlette.responses import JSONResponse

sdk_router = APIRouter(prefix="/sdk", tags=["sdk"])


@sdk_router.post("/allocate-key")
async def allocate_key(request: Request) -> JSONResponse:
    """Allocate an available API key from the pool.

    Returns the real API key for direct SDK use.
    The caller MUST report usage via /sdk/report-usage after each API call,
    and report errors via /sdk/report-error on failures.
    """
    key_manager = request.app.state.key_manager
    selected_key = await key_manager.select_key()

    if selected_key is None:
        raise HTTPException(
            status_code=503,
            detail="All API keys exhausted",
            headers={"Retry-After": "60"},
        )

    return JSONResponse(
        content={
            "key_id": selected_key.id,
            "api_key": selected_key.key,
        }
    )


@sdk_router.post("/report-usage")
async def report_usage(request: Request) -> Dict[str, str]:
    """Report successful API usage for a previously allocated key.

    Body: {"key_id": "key_1"}
    """
    key_manager = request.app.state.key_manager
    body = await request.json()
    key_id = body.get("key_id")

    if not key_id:
        raise HTTPException(status_code=400, detail="key_id is required")

    if key_id not in key_manager.pool.keys:
        raise HTTPException(status_code=404, detail=f"Key {key_id} not found")

    await key_manager.record_request(key_id)
    return {"status": "recorded"}


@sdk_router.post("/report-error")
async def report_error(request: Request) -> Dict[str, str]:
    """Report an API error for a previously allocated key.

    Body: {"key_id": "key_1", "is_rpd_limit": false}
    """
    key_manager = request.app.state.key_manager
    body = await request.json()
    key_id = body.get("key_id")
    is_rpd_limit = body.get("is_rpd_limit", False)

    if not key_id:
        raise HTTPException(status_code=400, detail="key_id is required")

    if key_id not in key_manager.pool.keys:
        raise HTTPException(status_code=404, detail=f"Key {key_id} not found")

    await key_manager.record_error(key_id, is_rpd_limit=is_rpd_limit)
    return {"status": "recorded"}
