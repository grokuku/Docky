"""API key authentication for the Docky Agent service."""

import os

from fastapi import Request
from fastapi.responses import JSONResponse


def get_api_key() -> str:
    """Return the configured agent API key from the environment."""
    return os.environ.get("DOCKY_AGENT_API_KEY", "")


def verify_api_key(request: Request) -> bool:
    """Return ``True`` if the request carries a valid Bearer token."""
    api_key = get_api_key()
    if not api_key:
        return False
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        return token == api_key
    return False


def require_api_key(request: Request):
    """Middleware-like helper: returns a 401 JSONResponse if auth fails.

    Usage::

        auth_err = require_api_key(request)
        if auth_err:
            return auth_err
    """
    if not verify_api_key(request):
        return JSONResponse(
            status_code=401,
            content={"error": "Invalid or missing API key"},
        )
    return None


async def verify_api_key_ws(websocket) -> bool:
    """Return ``True`` if the WebSocket connection carries a valid API key.

    The key can be supplied via the ``api_key`` query parameter or the
    ``Authorization: Bearer <key>`` header (query param is preferred for
    browser WebSocket clients).
    """
    api_key = get_api_key()
    if not api_key:
        return False
    token = websocket.query_params.get("api_key", "")
    if not token:
        auth_header = websocket.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    return token == api_key