"""Containers / ports / events endpoints (``/api/containers*``, ``/api/ports``,
``/api/events``, ``/api/presence/heartbeat``).

Extracted from ``app.routes.api``. ``agent_manager`` is resolved through the
façade ``app.routes.api`` at call time (``_api()``) so the tests' monkeypatch
of ``app.routes.api.agent_manager`` keeps taking effect. ``_events_clients``
comes from ``api_helpers`` (shared with ``_broadcast_agent_event``).
"""

import asyncio
import json
import logging
import urllib.parse

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import JSONResponse

from app.routes.api_helpers import (
    _check_auth,
    _check_auth_ws,
    _unauthorized,
    _resolve_agent,
    _check_agent_error,
    _sse_action_response,
    _events_clients,
)

router = APIRouter()


def _api():
    """Résolution tardive du namespace app.routes.api (évite tout cycle)."""
    from app.routes import api
    return api


@router.get("/containers")
async def api_list_containers(request: Request, agent: str = Query("all")):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_manager = _api().agent_manager
    if agent == "all":
        containers = await agent_manager.get_cached_containers()
        if containers is None:
            # Premier appel, cache pas encore rempli → fetch direct
            containers = await agent_manager.get_all_containers()
        return containers
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    return await agent_manager.get_containers(agent_name)


@router.get("/containers/{container_id}")
async def api_get_container(
    request: Request, container_id: str, agent: str = Query(...)
):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    agent_manager = _api().agent_manager
    c = await agent_manager.get_container(agent_name, container_id)
    if c is None:
        return JSONResponse(status_code=404, content={"detail": "Container not found"})
    c["stats"] = await agent_manager.get_container_stats(agent_name, container_id)
    return c


# ---------------------------------------------------------------------------
# Actions - Containers
# ---------------------------------------------------------------------------

@router.post("/containers/{container_id}/start")
async def api_start_container(
    request: Request, container_id: str, agent: str = Query(...)
):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    ok = await _api().agent_manager.start_container(agent_name, container_id)
    return {"success": ok}


@router.post("/containers/{container_id}/stop")
async def api_stop_container(
    request: Request, container_id: str, agent: str = Query(...)
):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    ok = await _api().agent_manager.stop_container(agent_name, container_id)
    return {"success": ok}


@router.post("/containers/{container_id}/restart")
async def api_restart_container(
    request: Request, container_id: str, agent: str = Query(...)
):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    ok = await _api().agent_manager.restart_container(agent_name, container_id)
    return {"success": ok}


@router.get("/containers/{container_id}/edit-spec")
async def api_get_container_edit_spec(
    request: Request, container_id: str, agent: str = Query(...)
):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    spec = await _api().agent_manager.get_container_edit_spec(agent_name, container_id)
    if spec is None:
        return JSONResponse(status_code=404, content={"detail": "Container not found"})
    return spec


@router.post("/containers/{container_id}/update")
async def api_update_container(
    request: Request, container_id: str, agent: str = Query(...)
):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON body"})
    result = await _api().agent_manager.update_container(agent_name, container_id, data)
    # Check for agent-side errors
    err = _check_agent_error(result)
    return err if err is not None else result


@router.post("/containers/{container_id}/update-image")
async def api_update_container_image(
    request: Request, container_id: str, agent: str = Query(...)
):
    """Pull the latest image for a container and recreate it (⬆ button).

    Streams the progress as SSE to the browser.
    """
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    return _sse_action_response(agent_name, _api().agent_manager.stream_update_container_image(agent_name, container_id))


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

@router.get("/containers/{container_id}/logs")
async def api_container_logs(
    request: Request, container_id: str, tail: int = Query(100),
    agent: str = Query(...),
):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    lines = await _api().agent_manager.get_container_logs(agent_name, container_id, tail=tail)
    return {"lines": lines}


@router.websocket("/containers/{container_id}/logs/stream")
async def ws_container_logs_stream(websocket: WebSocket, container_id: str):
    """WebSocket for streaming container logs in real-time (terminal mode).

    Proxies the client WebSocket to the target agent's
    ``/agent/containers/{id}/logs/stream`` endpoint.  The agent's API key is
    injected server-side and never exposed to the browser.  The client may
    send a first JSON message ``{"tail": N}`` to request historical log
    lines; otherwise (or on timeout) it is follow-only (tail=0).
    """
    agent_manager = _api().agent_manager
    # Auth
    username = _check_auth_ws(websocket)
    if username is None:
        await websocket.close(code=4401)
        return

    # Get agent param
    agent = websocket.query_params.get("agent", "")
    if not agent:
        await websocket.close(code=4400)
        return

    agent_name, err = _resolve_agent(agent)
    if err is not None:
        await websocket.close(code=4403)
        return

    # Get agent URL and API key
    agent_info = agent_manager.agents.get(agent_name)
    if not agent_info:
        await websocket.close(code=4404)
        return

    agent_url = agent_info.get("url", "").rstrip("/")

    # Build target WS URL. The API key travels in the ``Authorization``
    # header (via the centralised agent helper), never in the URL query
    # string, so it cannot leak into proxy / access logs.
    ws_proto = "wss" if agent_url.startswith("https") else "ws"
    agent_path = agent_url.split("://", 1)[1] if "://" in agent_url else agent_url
    target_url = f"{ws_proto}://{agent_path}/agent/containers/{urllib.parse.quote(container_id, safe='')}/logs/stream"

    # Accept the client WebSocket
    await websocket.accept()

    # Read the optional first client message to learn the desired tail.
    tail = 0
    try:
        first = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
        try:
            data = json.loads(first)
            if isinstance(data, dict) and "tail" in data:
                # Borne basse ET haute : pas de DoS via tail=10^9.
                tail = min(max(0, int(data["tail"])), 5000)
        except (ValueError, TypeError):
            pass
    except Exception:
        pass
    # Toujours transmettre tail (même 0) : sinon le défaut agent Query(100)
    # rejoue 100 lignes → doublons côté terminal.
    target_url += f"?tail={tail}"

    try:
        import websockets as ws_lib
        connect_kwargs = agent_manager._agent_ws_connect_kwargs(agent_info)
        async with ws_lib.connect(target_url, **connect_kwargs) as agent_ws:
            async def client_to_agent():
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg["type"] == "websocket.disconnect":
                            break
                        if msg.get("bytes") is not None:
                            await agent_ws.send(msg["bytes"])
                        elif msg.get("text") is not None:
                            await agent_ws.send(msg["text"])
                except WebSocketDisconnect:
                    pass
                except Exception as e:
                    logger.debug("client_to_agent relay ended: %s", e)

            async def agent_to_client():
                try:
                    async for msg in agent_ws:
                        if isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            await websocket.send_text(msg)
                except Exception as e:
                    logger.debug("agent_to_client relay ended: %s", e)

            # Use FIRST_COMPLETED so that when either side closes,
            # we cancel the other and exit cleanly.
            tasks = [
                asyncio.create_task(client_to_agent()),
                asyncio.create_task(agent_to_client()),
            ]
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
    except WebSocketDisconnect:
        logger.debug("WS logs/stream client disconnected")
    except Exception as e:
        logger.warning("WS logs/stream proxy error: %s", e)
        try:
            await websocket.send_text(json.dumps({"error": str(e)}))
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@router.websocket("/events")
async def ws_events(websocket: WebSocket):
    """Stream events to frontends. Frontend sends heartbeat as text."""
    username = _check_auth_ws(websocket)
    if username is None:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    _events_clients.append(websocket)
    try:
        while True:
            await websocket.receive_text()  # heartbeat
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in _events_clients:
            _events_clients.remove(websocket)


@router.post("/presence/heartbeat")
async def api_presence_heartbeat(request: Request):
    """Frontend heartbeat — keeps presence counter alive."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Console (exec)
# ---------------------------------------------------------------------------

@router.websocket("/containers/{container_id}/exec")
async def ws_container_exec(websocket: WebSocket, container_id: str):
    """WebSocket for interactive exec in a container (bidirectional).

    Proxies the client WebSocket to the target agent's
    ``/agent/containers/{id}/exec`` endpoint, relaying all messages in
    both directions for an interactive PTY shell.
    """
    agent_manager = _api().agent_manager
    # Auth
    username = _check_auth_ws(websocket)
    if username is None:
        await websocket.close(code=4401)
        return

    # Get agent param
    agent = websocket.query_params.get("agent", "")
    if not agent:
        await websocket.close(code=4400)
        return

    agent_name, err = _resolve_agent(agent)
    if err is not None:
        await websocket.close(code=4403)
        return

    # Get agent URL and API key
    agent_info = agent_manager.agents.get(agent_name)
    if not agent_info:
        await websocket.close(code=4404)
        return

    agent_url = agent_info.get("url", "").rstrip("/")

    # Build target WS URL. The API key travels in the ``Authorization``
    # header (via the centralised agent helper), never in the URL query
    # string, so it cannot leak into proxy / access logs.
    ws_proto = "wss" if agent_url.startswith("https") else "ws"
    agent_path = agent_url.split("://", 1)[1] if "://" in agent_url else agent_url
    target_url = f"{ws_proto}://{agent_path}/agent/containers/{urllib.parse.quote(container_id, safe='')}/exec"

    # Accept the client WebSocket
    await websocket.accept()

    try:
        import websockets as ws_lib
        connect_kwargs = agent_manager._agent_ws_connect_kwargs(agent_info)
        async with ws_lib.connect(target_url, **connect_kwargs) as agent_ws:
            async def client_to_agent():
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg["type"] == "websocket.disconnect":
                            break
                        if msg.get("bytes") is not None:
                            await agent_ws.send(msg["bytes"])
                        elif msg.get("text") is not None:
                            await agent_ws.send(msg["text"])
                except WebSocketDisconnect:
                    pass
                except Exception as e:
                    logger.debug("client_to_agent relay ended: %s", e)

            async def agent_to_client():
                try:
                    async for msg in agent_ws:
                        if isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            await websocket.send_text(msg)
                except Exception as e:
                    logger.debug("agent_to_client relay ended: %s", e)

            # Use FIRST_COMPLETED so that when either side closes,
            # we cancel the other and exit cleanly.
            tasks = [
                asyncio.create_task(client_to_agent()),
                asyncio.create_task(agent_to_client()),
            ]
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
    except Exception as e:
        logger.warning("WS exec proxy error: %s", e)
        try:
            await websocket.send_text(json.dumps({"error": str(e)}))
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@router.post("/containers/{container_id}/exec")
async def api_container_exec(
    request: Request, container_id: str, agent: str = Query(...)
):
    """Execute a one-shot command in a container via the agent.

    Body JSON: ``{ "command": "ls -la" }``
    Returns: ``{ "success": true, "output": "..." }``
    """
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON body"})
    command = data.get("command", "")
    if not command:
        return JSONResponse(status_code=400, content={"detail": "command is required"})
    try:
        result = await _api().agent_manager.exec_container(agent_name, container_id, command)
        if isinstance(result, dict) and not result.get("success", True):
            return JSONResponse(
                status_code=500,
                content={"detail": f"Exec error: {result.get('error', 'unknown')}"},
            )
        return result
    except Exception as exc:
        return JSONResponse(status_code=500, content={"detail": f"Exec error: {exc}"})


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

@router.get("/containers/{container_id}/stats")
async def api_container_stats(
    request: Request, container_id: str, agent: str = Query(...)
):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    return await _api().agent_manager.get_container_stats(agent_name, container_id)


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------

@router.get("/ports")
async def api_get_ports(request: Request, agent: str = Query("all")):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_manager = _api().agent_manager
    if agent == "all":
        ports = await agent_manager.get_cached_ports()
        if ports is None:
            ports = await agent_manager.get_all_ports()
        return ports
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    return await agent_manager.get_ports(agent_name)


# ---------------------------------------------------------------------------
# Update check
# ---------------------------------------------------------------------------

@router.get("/containers/{container_id}/update-check")
async def api_update_check(
    request: Request, container_id: str, agent: str = Query(...)
):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    return await _api().agent_manager.check_update(agent_name, container_id)
