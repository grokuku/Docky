"""Shared helpers for the API routers (extracted from ``app.routes.api``).

Module de helpers partagés par tous les routeurs de ``app.routes``. La façade
``app.routes.api`` les ré-exporte dans son namespace pour préserver les
imports de ``app.main`` et les monkeypatches des tests.

Règle de la façade : ``_resolve_agent`` et ``_sse_action_response`` dépendent
de ``agent_manager``, monkeypatché par les tests sur ``app.routes.api`` — il
est donc résolu au moment de l'appel via ``_api()`` (résolution tardive, aucun
cycle d'import).
"""

import asyncio
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

from fastapi import Request, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse

from app.auth.router import COOKIE_NAME
from app.auth.jwt_utils import verify_token
from app.version import _find_version_path, _VERSION_PATH  # noqa: F401  (ré-export)


def _api():
    """Résolution tardive du namespace app.routes.api (évite tout cycle)."""
    from app.routes import api
    return api


# Module-level list of WebSocket clients listening for agent events
_events_clients: list = []


async def _broadcast_agent_event(agent_name: str, action: str):
    """Broadcast a Docker event to every connected frontend.

    Injected into ``agent_manager.broadcast_agent_event`` so the agent manager
    never has to import ``app.routes.api`` (which imports it back at module
    level — the latent circular dependency this refactor removes).
    """
    for ws in list(_events_clients):
        try:
            await ws.send_json({"type": "docky_event", "agent": agent_name, "action": action})
        except Exception:
            pass


# La résolution du chemin de ``version.txt`` vit désormais dans le module
# partagé ``app.version`` (utilisé aussi par ``app.main`` et ``app/__init__``).
# Les noms ``_find_version_path`` / ``_VERSION_PATH`` sont ré-exportés ici pour
# préserver les imports existants (façade ``app.routes.api``, ``settings.py``,
# tests).


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _check_auth(request: Request) -> Optional[str]:
    """Return username if authenticated, else None."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return verify_token(token)


def _check_auth_ws(websocket: WebSocket) -> Optional[str]:
    """Check auth for a WebSocket via cookie (sent during handshake)."""
    token = websocket.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return verify_token(token)


def _unauthorized() -> JSONResponse:
    return JSONResponse(status_code=401, content={"detail": "Unauthorized"})


# ---------------------------------------------------------------------------
# Agent helpers
# ---------------------------------------------------------------------------

def _agent_bad_request() -> JSONResponse:
    return JSONResponse(
        status_code=400, content={"detail": "agent parameter required"}
    )


def _agent_not_found(name: str) -> JSONResponse:
    return JSONResponse(
        status_code=404, content={"detail": f"Agent '{name}' not found"}
    )


def _agent_offline(name: str) -> JSONResponse:
    return JSONResponse(
        status_code=503, content={"detail": f"Agent '{name}' is offline"}
    )


def _agent_unreachable(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content={"detail": f"Failed to communicate with agent: {detail}"},
    )


def _resolve_agent(agent_name: Optional[str]):
    """Validate ``agent_name`` and return ``(agent_name, error_response)``.

    On success ``error_response`` is ``None``; on failure ``agent_name`` is
    ``None`` and a ready-to-return ``JSONResponse`` is provided.

    ``agent_manager`` is resolved via the façade namespace at call time so the
    ``app.routes.api.agent_manager`` monkeypatch from the tests keeps effect.
    """
    agent_manager = _api().agent_manager
    if not agent_name:
        return None, _agent_bad_request()
    if agent_name not in agent_manager.agents:
        return None, _agent_not_found(agent_name)
    if agent_manager.agents[agent_name]["status"] == "offline":
        return None, _agent_offline(agent_name)
    return agent_name, None


def _check_agent_error(result):
    """If *result* is a dict reporting an agent-side error, return an HTTP error.

    Distinguishes two failure modes:

    - **agent unreachable** (transport / HTTP error, tagged ``unreachable``)
      → 502 “Failed to communicate with agent”.
    - **business error returned by the agent** (``{success: false, error: msg}``)
      → 500 with ``detail`` set to the real message so the UI no longer masks
      the underlying Docker/compose error.

    The response body always carries ``success: False`` so JSON consumers that
    inspect ``result.success`` (e.g. ``applyContainerEdit``) keep working and
    can display ``result.error``.
    """
    if isinstance(result, dict) and not result.get("success", True) and result.get("error"):
        if result.get("unreachable"):
            return _agent_unreachable(str(result["error"]))
        message = str(result["error"])
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": message, "detail": message},
        )
    return None


def _sse_response(event_iter, on_success=None):
    """Wrap an async iterator of stream events into an SSE response.

    Re-emits the events produced by ``agent_manager.stream_*`` methods
    (``output`` / ``done`` / ``error``) as ``text/event-stream`` frames to the
    browser. The ``Cache-Control: no-cache`` and ``X-Accel-Buffering: no``
    headers prevent buffering by browsers and reverse proxies.

    If *on_success* is provided, it is awaited (once) after the stream ends and
    the final ``done`` event reported success. It is used to invalidate the
    agent cache so the UI reflects the new state immediately.
    """
    async def generate():
        success = False
        try:
            async for evt in event_iter:
                if evt["type"] == "output":
                    yield f"event: output\ndata: {json.dumps({'line': evt.get('line', '')}, ensure_ascii=False)}\n\n"
                elif evt["type"] == "done":
                    success = bool(evt.get("success", True))
                    payload = {"success": success, "output": evt.get("output", "")}
                    if evt.get("error"):
                        payload["error"] = evt["error"]
                    yield f"event: done\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                elif evt["type"] == "error":
                    yield f"event: error\ndata: {json.dumps({'error': evt.get('error', 'Erreur inconnue')}, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
        finally:
            if success and on_success is not None:
                try:
                    await on_success()
                except Exception as e:
                    logger.warning("SSE on_success callback failed: %s", e)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _sse_action_response(agent_name: str, event_iter):
    """SSE response for an action that invalidates the agent cache on success."""
    async def _on_success():
        await _api().agent_manager.invalidate_cache(agent_name)

    return _sse_response(event_iter, on_success=_on_success)


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

def _mask_api_key(key: str) -> str:
    """Mask an API key, showing only the last 4 characters."""
    if not key:
        return ""
    if len(key) <= 4:
        return "****"
    return "****" + key[-4:]
