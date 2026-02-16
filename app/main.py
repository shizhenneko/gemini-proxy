"""FastAPI application for Gemini API key pool proxy."""

import logging
from contextlib import asynccontextmanager
from typing import Dict

import httpx
from fastapi import FastAPI, Request

from app.config import load_config
from app.key_manager import KeyManager
from app.proxy import proxy_request
from app.admin import admin_router
from app.sdk_support import sdk_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle: startup and shutdown."""
    config = load_config()

    logging.basicConfig(level=getattr(logging, config.log_level))

    http_client = httpx.AsyncClient(
        base_url=config.gemini_base_url,
        timeout=httpx.Timeout(10.0, read=300.0, write=30.0),
        limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
    )

    key_manager = KeyManager(config)

    app.state.config = config
    app.state.http_client = http_client
    app.state.key_manager = key_manager

    logger.info("Gemini proxy started with %d keys", len(config.api_keys))

    yield

    await http_client.aclose()
    logger.info("Gemini proxy stopped")


app = FastAPI(title="Gemini API Key Pool Proxy", lifespan=lifespan)

# Include routers BEFORE catch-all route
app.include_router(admin_router)
app.include_router(sdk_router)


@app.get("/")
async def root(request: Request) -> Dict[str, object]:
    key_manager = request.app.state.key_manager
    status = await key_manager.get_status()
    return {
        "service": "Gemini API Key Pool Proxy",
        "status": "running",
        "keys_available": status["available_keys"],
        "total_keys": status["total_keys"],
    }


@app.get("/health")
async def health_check(request: Request) -> Dict[str, object]:
    """Health check endpoint with key pool status."""
    key_manager = request.app.state.key_manager
    status = await key_manager.get_status()
    return {
        "status": "healthy",
        "keys_available": status["available_keys"],
        "total_keys": status["total_keys"],
    }


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_endpoint(request: Request, path: str):
    """Catch-all proxy endpoint that forwards requests to Gemini API."""
    return await proxy_request(
        request=request,
        key_manager=request.app.state.key_manager,
        http_client=request.app.state.http_client,
        config=request.app.state.config,
    )
