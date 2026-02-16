"""Admin endpoints for key management."""

from typing import Dict

from fastapi import APIRouter, Request, HTTPException
from starlette.responses import Response, JSONResponse

admin_router = APIRouter(prefix="/admin", tags=["admin"])


@admin_router.get("/status")
async def get_all_status(request: Request) -> Dict[str, object]:
    """Get status of all API keys in the pool."""
    key_manager = request.app.state.key_manager
    return await key_manager.get_status()


@admin_router.get("/status/{key_id}")
async def get_key_status(request: Request, key_id: str) -> Dict[str, object]:
    """Get status of a specific API key."""
    key_manager = request.app.state.key_manager
    status = await key_manager.get_key_status(key_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"Key {key_id} not found")
    return status


@admin_router.post("/reset")
async def reset_counters(request: Request) -> Dict[str, str]:
    """Reset daily counters for all keys."""
    key_manager = request.app.state.key_manager
    await key_manager.force_reset()
    return {"message": "Counters reset successfully"}


@admin_router.post("/keys")
async def add_key(request: Request) -> JSONResponse:
    """Add a new API key to the pool."""
    key_manager = request.app.state.key_manager
    body = await request.json()
    api_key = body.get("api_key")
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key is required")
    rpd_limit = body.get("rpd_limit", 250)
    rpm_limit = body.get("rpm_limit", 10)
    if not isinstance(rpd_limit, int) or rpd_limit <= 0:
        raise HTTPException(
            status_code=400, detail="rpd_limit must be a positive integer"
        )
    if not isinstance(rpm_limit, int) or rpm_limit <= 0:
        raise HTTPException(
            status_code=400, detail="rpm_limit must be a positive integer"
        )
    try:
        key_id = await key_manager.add_key(api_key, rpd_limit, rpm_limit)
        return JSONResponse(content={"key_id": key_id}, status_code=201)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@admin_router.delete("/keys/{key_id}")
async def remove_key(request: Request, key_id: str) -> Response:
    """Remove an API key from the pool."""
    key_manager = request.app.state.key_manager
    removed = await key_manager.remove_key(key_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Key {key_id} not found")
    return Response(status_code=204)
