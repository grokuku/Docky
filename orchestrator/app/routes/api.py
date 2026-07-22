"""API endpoints for Docky (JSON, JWT-protected).

The orchestrator no longer talks to Docker directly: every Docker-related
operation is delegated to a remote *agent* through ``agent_manager``. Each
request must specify which agent it targets (via the ``agent`` query
parameter or, for POST bodies, the ``agent`` field). The special value
``all`` aggregates data from every configured agent.
"""

import asyncio
import json
import logging
import urllib.parse
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

import httpx
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import JSONResponse, PlainTextResponse

from app.auth.router import COOKIE_NAME
from app.auth.jwt_utils import verify_token
from app.agent_manager.client import agent_manager
import bcrypt
from app.config import load_settings, save_settings, load_users, save_users
from app.llm.client import (
    LLMClient,
    run_chat,
    read_soul,
    update_soul,
    execute_tool,
    build_system_prompt,
    TOOLS,
    HUMAN_VALIDATION_MARKER,
)

router = APIRouter(prefix="/api")

# Module-level list of WebSocket clients listening for agent events
_events_clients: list = []


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
    """
    if not agent_name:
        return None, _agent_bad_request()
    if agent_name not in agent_manager.agents:
        return None, _agent_not_found(agent_name)
    if agent_manager.agents[agent_name]["status"] == "offline":
        return None, _agent_offline(agent_name)
    return agent_name, None


def _check_agent_error(result):
    """If *result* is a dict reporting an agent-side error, return a 502."""
    if isinstance(result, dict) and not result.get("success", True) and result.get("error"):
        return _agent_unreachable(str(result["error"]))
    return None


# ---------------------------------------------------------------------------
# Agents management
# ---------------------------------------------------------------------------

@router.get("/agents")
async def api_list_agents(request: Request):
    """List all configured agents with their current status."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    await agent_manager.ping_all()
    return agent_manager.list_agents()


@router.post("/agents/refresh")
async def api_refresh_agents(request: Request):
    """Force a status refresh (ping) of all agents."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    await agent_manager.ping_all()
    return {"success": True, "agents": agent_manager.list_agents()}


@router.get("/agents/{name}/containers")
async def api_agent_containers(request: Request, name: str):
    """List containers belonging to a specific agent."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(name)
    if err is not None:
        return err
    return await agent_manager.get_containers(agent_name)


@router.get("/agents/{name}/stacks")
async def api_agent_stacks(request: Request, name: str):
    """List stacks belonging to a specific agent."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(name)
    if err is not None:
        return err
    return await agent_manager.get_stacks(agent_name)


@router.get("/agents/{name}/ports")
async def api_agent_ports(request: Request, name: str):
    """List ports in use on a specific agent host."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(name)
    if err is not None:
        return err
    return await agent_manager.get_ports(agent_name)


# ---------------------------------------------------------------------------
# Settings - LLM configuration
# ---------------------------------------------------------------------------

def _mask_api_key(key: str) -> str:
    """Mask an API key, showing only the last 4 characters."""
    if not key:
        return ""
    if len(key) <= 4:
        return "****"
    return "****" + key[-4:]


@router.get("/settings/llm")
async def api_get_llm_settings(request: Request):
    """Return the LLM configuration with the API key partially masked."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    settings = load_settings()
    llm = settings.get("llm", {}) or {}
    firecrawl = settings.get("firecrawl", {}) or {}
    return {
        "endpoint": llm.get("endpoint", ""),
        "api_key": _mask_api_key(llm.get("api_key", "")),
        "model": llm.get("model", ""),
        "firecrawl_endpoint": firecrawl.get("endpoint", ""),
        "firecrawl_key": _mask_api_key(firecrawl.get("api_key", "")),
    }


@router.put("/settings/llm")
async def api_update_llm_settings(request: Request):
    """Update the LLM (and firecrawl) configuration in settings.yaml.

    If the provided ``api_key`` or ``firecrawl_key`` is empty or looks like a
    masked value (``****xxxx``), the previously stored value is preserved.
    """
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON body"})

    settings = load_settings()
    llm = settings.get("llm") or {}
    firecrawl = settings.get("firecrawl") or {}

    endpoint = data.get("endpoint", llm.get("endpoint", ""))
    model = data.get("model", llm.get("model", ""))

    new_api_key = data.get("api_key", "")
    if not new_api_key or new_api_key.startswith("****"):
        api_key = llm.get("api_key", "")
    else:
        api_key = new_api_key

    new_firecrawl_key = data.get("firecrawl_key", "")
    if not new_firecrawl_key or new_firecrawl_key.startswith("****"):
        firecrawl_key = firecrawl.get("api_key", "")
    else:
        firecrawl_key = new_firecrawl_key

    # Firecrawl endpoint (optional, self-hosted WebClaw)
    firecrawl_endpoint = data.get("firecrawl_endpoint", firecrawl.get("endpoint", ""))

    settings["llm"] = {"endpoint": endpoint, "api_key": api_key, "model": model}
    settings["firecrawl"] = {"endpoint": firecrawl_endpoint, "api_key": firecrawl_key}
    save_settings(settings)
    return {"success": True}


@router.post("/settings/llm/models")
async def scan_llm_models(request: Request):
    """Scan the LLM endpoint for available models (OpenAI-compatible /models)."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON body"})

    endpoint = (data.get("endpoint") or "").strip()
    api_key = data.get("api_key", "")

    if not endpoint:
        return JSONResponse(
            status_code=400,
            content={"success": False, "models": [], "error": "endpoint is required"},
        )

    # If api_key is masked/empty, fall back to the stored value.
    if not api_key or api_key.startswith("****"):
        settings = load_settings()
        api_key = settings.get("llm", {}).get("api_key", "")

    # Build the /models URL. Most OpenAI-compatible APIs expose /v1/models,
    # but some (e.g. LM Studio, certain proxies) expose /models directly.
    base = endpoint.rstrip("/")
    if base.endswith("/models"):
        url = base
    elif base.endswith("/v1"):
        url = base + "/models"
    else:
        url = base + "/v1/models"

    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            payload = resp.json()
        # OpenAI format: { "data": [{ "id": "model-name", ... }, ...] }
        models = [m["id"] for m in payload.get("data", []) if m.get("id")]
        return {"success": True, "models": models}
    except Exception as exc:
        return {"success": False, "models": [], "error": str(exc)}


@router.post("/settings/llm/test")
async def api_test_llm(request: Request):
    """Test the LLM connection by sending a simple "Hello" chat request."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    llm = LLMClient()
    if not llm.is_configured():
        return JSONResponse(
            status_code=400,
            content={"success": False, "detail": "LLM is not configured (endpoint/model missing)."},
        )
    try:
        result = await llm.chat([{"role": "user", "content": "Hello"}])
        # The response may contain choices; just confirm we got something back.
        choices = result.get("choices") if isinstance(result, dict) else None
        if choices is not None:
            return {"success": True, "detail": "Connection successful."}
        return JSONResponse(
            status_code=502,
            content={"success": False, "detail": f"Unexpected response: {result}"},
        )
    except Exception as exc:
        return JSONResponse(
            status_code=502,
            content={"success": False, "detail": f"LLM error: {exc}"},
        )


# ---------------------------------------------------------------------------
# Settings - Agents management
# ---------------------------------------------------------------------------

def _save_agents(agents: list):
    """Persist the agents list into settings.yaml."""
    settings = load_settings()
    settings["agents"] = agents
    save_settings(settings)


@router.get("/settings/agents")
async def api_get_settings_agents(request: Request):
    """List configured agents (name, url, masked api_key, status)."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    settings = load_settings()
    agents = settings.get("agents", []) or []
    result = []
    for a in agents:
        result.append({
            "name": a.get("name", ""),
            "url": a.get("url", ""),
            "api_key": _mask_api_key(a.get("api_key", "")),
            "path_mappings": a.get("path_mappings", []) or [],
            "status": agent_manager.agents.get(a.get("name", ""), {}).get("status", "unknown"),
        })
    return result


@router.post("/settings/agents")
async def api_add_settings_agent(request: Request):
    """Add a new agent to settings.yaml and reload the agent manager."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON body"})
    name = (data.get("name") or "").strip()
    url = (data.get("url") or "").strip()
    api_key = data.get("api_key") or ""
    if not name or not url:
        return JSONResponse(status_code=400, content={"detail": "name and url are required"})
    settings = load_settings()
    agents = settings.get("agents", []) or []
    if any(a.get("name") == name for a in agents):
        return JSONResponse(status_code=409, content={"detail": f"Agent '{name}' already exists"})
    agents.append({"name": name, "url": url, "api_key": api_key, "path_mappings": data.get("path_mappings", []) or []})
    settings["agents"] = agents
    save_settings(settings)
    agent_manager.reload()
    return {"success": True}


@router.put("/settings/agents/{name}")
async def api_update_settings_agent(request: Request, name: str):
    """Modify an existing agent in settings.yaml and reload the agent manager."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON body"})
    settings = load_settings()
    agents = settings.get("agents", []) or []
    found = None
    for a in agents:
        if a.get("name") == name:
            found = a
            break
    if found is None:
        return JSONResponse(status_code=404, content={"detail": f"Agent '{name}' not found"})
    new_name = (data.get("name") or name).strip()
    new_url = (data.get("url") or found.get("url", "")).strip()
    new_key = data.get("api_key")
    if not new_key or new_key.startswith("****"):
        new_key = found.get("api_key", "")
    # If the name changed, make sure it does not collide with another agent.
    if new_name != name and any(a.get("name") == new_name for a in agents):
        return JSONResponse(status_code=409, content={"detail": f"Agent '{new_name}' already exists"})
    found["name"] = new_name
    found["url"] = new_url
    found["api_key"] = new_key
    found["path_mappings"] = data.get("path_mappings", found.get("path_mappings", []) or [])
    save_settings(settings)
    agent_manager.reload()
    return {"success": True}


@router.delete("/settings/agents/{name}")
async def api_delete_settings_agent(request: Request, name: str):
    """Remove an agent from settings.yaml and reload the agent manager."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    settings = load_settings()
    agents = settings.get("agents", []) or []
    new_agents = [a for a in agents if a.get("name") != name]
    if len(new_agents) == len(agents):
        return JSONResponse(status_code=404, content={"detail": f"Agent '{name}' not found"})
    settings["agents"] = new_agents
    save_settings(settings)
    agent_manager.reload()
    return {"success": True}


@router.post("/settings/agents/{name}/test")
async def api_test_settings_agent(request: Request, name: str):
    """Ping an agent to verify the connection."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    # Make sure the manager has the latest configuration before pinging.
    agent_manager.reload()
    if name not in agent_manager.agents:
        return JSONResponse(status_code=404, content={"detail": f"Agent '{name}' not found"})
    online = await agent_manager.ping_agent(name)
    return {"success": online, "status": agent_manager.agents[name]["status"]}


# ---------------------------------------------------------------------------
# Settings - Password change
# ---------------------------------------------------------------------------

@router.put("/settings/password")
async def api_change_password(request: Request):
    """Change the current user's password.

    Body JSON: ``{ "current_password": "...", "new_password": "..." }``
    """
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON body"})

    current_password = data.get("current_password", "")
    new_password = data.get("new_password", "")

    if not current_password or not new_password:
        return JSONResponse(
            status_code=400, content={"detail": "current_password and new_password are required"}
        )

    if len(new_password) < 6:
        return JSONResponse(status_code=400, content={"detail": "Password too short"})

    # Load users.yaml and find the current user.
    users_data = load_users()
    users_list = users_data.get("users", []) or []
    target = None
    for user in users_list:
        if user.get("username") == username:
            target = user
            break

    if target is None:
        return JSONResponse(status_code=404, content={"detail": "User not found"})

    stored_hash = target.get("password_hash", "")
    if not stored_hash or not bcrypt.checkpw(
        current_password.encode("utf-8"), stored_hash.encode("utf-8")
    ):
        return JSONResponse(
            status_code=400, content={"detail": "Current password is incorrect"}
        )

    # Hash the new password and persist.
    new_hash = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    target["password_hash"] = new_hash
    save_users(users_data)
    return {"success": True}


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------

@router.get("/version")
async def api_version(request: Request):
    """Return the current Docky version from version.txt."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    # Look for version.txt relative to the app file
    version_path = Path(__file__).parent.parent.parent.parent.parent / "version.txt"
    try:
        content = version_path.read_text().strip()
        return {"version": content}
    except Exception:
        return {"version": "0.0.1"}


@router.get("/version-check")
async def api_version_check(request: Request):
    """Compare orchestrator version with each agent's version.

    Returns the orchestrator version, each agent's version, and a list of
    mismatches (agents whose version differs from the orchestrator).
    """
    username = _check_auth(request)
    if username is None:
        return _unauthorized()

    # Read orchestrator version
    orch_version = "unknown"
    try:
        orch_version = (Path(__file__).parent.parent.parent.parent.parent / 'version.txt').read_text().strip()
    except Exception:
        pass

    # Fetch versions from all agents
    agent_versions = {}
    for name in agent_manager.agents:
        try:
            health = await agent_manager._request(name, "GET", "/agent/health")
            if isinstance(health, dict):
                agent_versions[name] = health.get("version", "unknown")
        except Exception:
            agent_versions[name] = "unreachable"

    # Detect mismatches
    mismatches = []
    for agent, ver in agent_versions.items():
        if ver != "unreachable" and ver != orch_version:
            mismatches.append({
                "agent": agent,
                "agent_version": ver,
                "orchestrator_version": orch_version,
            })

    return {
        "orchestrator_version": orch_version,
        "agents": agent_versions,
        "mismatches": mismatches,
    }


# ---------------------------------------------------------------------------
# Settings - Git history
# ---------------------------------------------------------------------------

@router.get("/settings/git-history")
async def api_get_git_history_settings(request: Request):
    """Return the git/history retention settings."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    from app.config import load_settings
    settings = load_settings()
    return settings.get('history_retention', {'max_versions': 50})


@router.put("/settings/git-history")
async def api_update_git_history_settings(request: Request):
    """Update the git/history retention settings."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON"})
    from app.config import load_settings, save_settings
    settings = load_settings()
    settings['history_retention'] = {'max_versions': data.get('max_versions', 50)}
    save_settings(settings)
    return {"success": True}


# ---------------------------------------------------------------------------
# Settings - Stacks metadata
# ---------------------------------------------------------------------------

@router.get("/settings/stacks-meta")
async def api_get_stacks_meta(request: Request):
    """Return the stacks metadata (family, sort, grouping)."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    from app.config import load_settings
    settings = load_settings()
    return settings.get('stacks_meta', {})


@router.put("/settings/stacks-meta")
async def api_update_stacks_meta(request: Request):
    """Update the stacks metadata (family, sort, grouping)."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON"})
    from app.config import load_settings, save_settings
    settings = load_settings()
    settings['stacks_meta'] = data
    save_settings(settings)
    return {"success": True}


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------

@router.get("/containers")
async def api_list_containers(request: Request, agent: str = Query("all")):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
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
    ok = await agent_manager.start_container(agent_name, container_id)
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
    ok = await agent_manager.stop_container(agent_name, container_id)
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
    ok = await agent_manager.restart_container(agent_name, container_id)
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
    spec = await agent_manager.get_container_edit_spec(agent_name, container_id)
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
    result = await agent_manager.update_container(agent_name, container_id, data)
    # Check for agent-side errors
    err = _check_agent_error(result)
    return err if err is not None else result


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
    lines = await agent_manager.get_container_logs(agent_name, container_id, tail=tail)
    return {"lines": lines}


@router.websocket("/containers/{container_id}/logs/stream")
async def ws_container_logs(websocket: WebSocket, container_id: str):
    """WebSocket for streaming container logs in real-time.

    Proxies the client WebSocket to the target agent's
    ``/agent/containers/{id}/logs/stream`` endpoint, relaying all messages
    bidirectionally so the frontend gets a live log stream.
    """
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
    agent_api_key = agent_info.get("api_key", "")

    # Build target WS URL
    ws_proto = "wss" if agent_url.startswith("https") else "ws"
    agent_path = agent_url.split("://", 1)[1] if "://" in agent_url else agent_url
    target_url = f"{ws_proto}://{agent_path}/agent/containers/{urllib.parse.quote(container_id, safe='')}/logs/stream"
    if agent_api_key:
        target_url += f"?api_key={urllib.parse.quote(agent_api_key, safe='')}"

    # Accept the client WebSocket
    await websocket.accept()

    try:
        import websockets as ws_lib
        async with ws_lib.connect(target_url) as agent_ws:
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
        logger.warning("WS logs proxy error: %s", e)
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
    agent_api_key = agent_info.get("api_key", "")

    # Build target WS URL
    ws_proto = "wss" if agent_url.startswith("https") else "ws"
    agent_path = agent_url.split("://", 1)[1] if "://" in agent_url else agent_url
    target_url = f"{ws_proto}://{agent_path}/agent/containers/{urllib.parse.quote(container_id, safe='')}/exec"
    if agent_api_key:
        target_url += f"?api_key={urllib.parse.quote(agent_api_key, safe='')}"

    # Accept the client WebSocket
    await websocket.accept()

    try:
        import websockets as ws_lib
        async with ws_lib.connect(target_url) as agent_ws:
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
        result = await agent_manager.exec_container(agent_name, container_id, command)
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
    return await agent_manager.get_container_stats(agent_name, container_id)


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------

@router.get("/ports")
async def api_get_ports(request: Request, agent: str = Query("all")):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
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
    return await agent_manager.check_update(agent_name, container_id)


# ---------------------------------------------------------------------------
# Stacks
# ---------------------------------------------------------------------------

@router.get("/stacks")
async def api_list_stacks(request: Request, agent: str = Query("all")):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    if agent == "all":
        stacks = await agent_manager.get_cached_stacks()
        if stacks is None:
            stacks = await agent_manager.get_all_stacks()
        return stacks
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    return await agent_manager.get_stacks(agent_name)


@router.get("/stacks/{name}/containers")
async def api_stack_containers(
    request: Request, name: str, agent: str = Query(...)
):
    """List containers belonging to a given stack on an agent.

    The agent does not expose a dedicated stack-containers endpoint, so we
    filter the agent's full container list by stack label/name.
    """
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    containers = await agent_manager.get_containers(agent_name)
    result = []
    # The special "Standalone" pseudo-stack groups every container that is
    # not part of any Docker Compose project.
    standalone = name == "Standalone"
    for c in containers:
        labels = c.get("labels", {}) if isinstance(c, dict) else {}
        stack_label = labels.get("com.docker.compose.project") or c.get("stack")
        if standalone:
            if not stack_label:
                result.append(c)
        elif stack_label and stack_label == name:
            result.append(c)
    return result


# ---------------------------------------------------------------------------
# Stack actions
# ---------------------------------------------------------------------------

@router.post("/stacks/{name}/start")
async def api_stack_start(request: Request, name: str, agent: str = Query(...)):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    result = await agent_manager.start_stack(agent_name, name)
    err = _check_agent_error(result)
    return err if err is not None else result


@router.post("/stacks/{name}/stop")
async def api_stack_stop(request: Request, name: str, agent: str = Query(...)):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    result = await agent_manager.stop_stack(agent_name, name)
    err = _check_agent_error(result)
    return err if err is not None else result


@router.post("/stacks/{name}/restart")
async def api_stack_restart(request: Request, name: str, agent: str = Query(...)):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    result = await agent_manager.restart_stack(agent_name, name)
    err = _check_agent_error(result)
    return err if err is not None else result


@router.post("/stacks/{name}/update")
async def api_stack_update(request: Request, name: str, agent: str = Query(...)):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    try:
        result = await agent_manager.update_stack(agent_name, name)
        return result
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ---------------------------------------------------------------------------
# Stack files (editor)
# ---------------------------------------------------------------------------

@router.get("/stacks/{name}/files")
async def api_list_stack_files(request: Request, name: str, agent: str = Query(...)):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    files = await agent_manager.get_stack_files(agent_name, name)
    return {"files": files}


@router.get("/stacks/{name}/files-with-content")
async def api_list_stack_files_with_content(
    request: Request, name: str, agent: str = Query(...)
):
    """List all files in a stack WITH their content in a single request.

    This avoids N+1 calls (1 list + N file reads) by returning everything
    in one batch.  Falls back gracefully if the agent does not support the
    endpoint (returns a 404).
    """
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    try:
        result = await agent_manager.get_stack_files_with_content(agent_name, name)
        return result
    except Exception as e:
        logger.warning("files-with-content failed for %s/%s: %s", agent_name, name, e)
        return JSONResponse(status_code=502, content={"error": str(e), "files": []})


@router.get("/stacks/{name}/files/{filename:path}")
async def api_get_stack_file(
    request: Request, name: str, filename: str, agent: str = Query(...)
):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    content = await agent_manager.get_stack_file(agent_name, name, filename)
    if content is None:
        return JSONResponse(status_code=404, content={"detail": "File not found"})
    return PlainTextResponse(content)


@router.put("/stacks/{name}/files/{filename:path}")
async def api_put_stack_file(
    request: Request, name: str, filename: str, agent: str = Query(...)
):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    body = await request.body()
    content = body.decode("utf-8")
    ok = await agent_manager.save_stack_file(agent_name, name, filename, content)
    if not ok:
        return JSONResponse(status_code=502, content={"detail": "Failed to communicate with agent"})
    return {"success": True, "name": filename}


@router.put("/stacks/{name}/files/{filename}/permissions")
async def api_set_file_permissions(
    request: Request, name: str, filename: str, agent: str = Query(...)
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
    mode = data.get("mode")
    if mode is None:
        return JSONResponse(status_code=400, content={"detail": "mode is required"})
    result = await agent_manager.set_permissions(agent_name, name, filename, mode)
    err = _check_agent_error(result)
    return err if err is not None else result


# ---------------------------------------------------------------------------
# Compose / env shortcuts
# ---------------------------------------------------------------------------

@router.get("/stacks/{name}/compose")
async def api_get_compose(request: Request, name: str, agent: str = Query(...)):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    content = await agent_manager.get_stack_file(agent_name, name, "docker-compose.yml")
    if content is None:
        return JSONResponse(status_code=404, content={"detail": "Compose file not found"})
    return PlainTextResponse(content)


@router.put("/stacks/{name}/compose")
async def api_put_compose(request: Request, name: str, agent: str = Query(...)):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    body = await request.body()
    content = body.decode("utf-8")
    ok = await agent_manager.save_stack_file(agent_name, name, "docker-compose.yml", content)
    if not ok:
        return JSONResponse(status_code=502, content={"detail": "Failed to communicate with agent"})
    return {"success": True}


@router.get("/stacks/{name}/env")
async def api_get_env(request: Request, name: str, agent: str = Query(...)):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    content = await agent_manager.get_stack_file(agent_name, name, ".env")
    if content is None:
        return JSONResponse(status_code=404, content={"detail": ".env file not found"})
    return PlainTextResponse(content)


@router.put("/stacks/{name}/env")
async def api_put_env(request: Request, name: str, agent: str = Query(...)):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    body = await request.body()
    content = body.decode("utf-8")
    ok = await agent_manager.save_stack_file(agent_name, name, ".env", content)
    if not ok:
        return JSONResponse(status_code=502, content={"detail": "Failed to communicate with agent"})
    return {"success": True}


# ---------------------------------------------------------------------------
# Stack lifecycle
# ---------------------------------------------------------------------------

@router.post("/stacks")
async def api_create_stack(request: Request, agent: str = Query(...)):
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
    name = data.get("name", "")
    compose = data.get("compose", "")
    env = data.get("env", "")
    if not name:
        return JSONResponse(status_code=400, content={"detail": "name is required"})
    result = await agent_manager.create_stack(agent_name, name, compose, env)
    err = _check_agent_error(result)
    return err if err is not None else result


@router.post("/stacks/import")
async def api_import_stack(request: Request, agent: str = Query(...)):
    """Import a stack from an external folder on an agent."""
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
    source_path = data.get("source_path", "")
    stack_name = data.get("stack_name")
    dry_run = data.get("dry_run", False)
    if not source_path:
        return JSONResponse(status_code=400, content={"detail": "source_path is required"})
    try:
        # Translate the source path using the agent's path mappings
        translated_path = agent_manager.translate_path(agent_name, source_path)
        result = await agent_manager.import_stack(agent_name, translated_path, stack_name, dry_run)
        return result
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.delete("/stacks/{name}")
async def api_delete_stack(request: Request, name: str, agent: str = Query(...)):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    result = await agent_manager.delete_stack(agent_name, name)
    err = _check_agent_error(result)
    return err if err is not None else result


@router.post("/stacks/{name}/deploy")
async def api_deploy_stack(request: Request, name: str, agent: str = Query(...)):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    try:
        result = await agent_manager.deploy_stack(agent_name, name)
        err = _check_agent_error(result)
        return err if err is not None else result
    except Exception as e:
        logger.error("deploy_stack failed for stack '%s' on agent '%s': %s", name, agent_name, str(e), exc_info=True)
        return JSONResponse(status_code=502, content={"detail": f"Failed to deploy stack: {str(e)}"})


# ---------------------------------------------------------------------------
# Git history
# ---------------------------------------------------------------------------

@router.get("/stacks/{name}/history")
async def api_stack_history(request: Request, name: str, agent: str = Query(...)):
    """Return the git history for a stack."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err:
        return err
    return await agent_manager.get_stack_history(agent_name, name)


@router.get("/stacks/{name}/history/{hash}")
async def api_stack_version(request: Request, name: str, hash: str, agent: str = Query(...)):
    """Return a specific git version for a stack."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err:
        return err
    return await agent_manager.get_stack_version(agent_name, name, hash)


@router.post("/stacks/{name}/history/restore/{hash}")
async def api_restore_stack(request: Request, name: str, hash: str, agent: str = Query(...)):
    """Restore a stack to a specific git version."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err:
        return err
    return await agent_manager.restore_stack_version(agent_name, name, hash)


# ---------------------------------------------------------------------------
# LLM Chat
# ---------------------------------------------------------------------------

@router.post("/chat")
async def chat_endpoint(request: Request):
    """Main chat endpoint: send a message, get the LLM response."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON body"})
    message = data.get("message")
    if not message:
        return JSONResponse(status_code=400, content={"detail": "message is required"})
    history = data.get("history") or []

    llm = LLMClient()
    if not llm.is_configured():
        return JSONResponse(
            status_code=400,
            content={"detail": "LLM is not configured. Set llm.endpoint and llm.model in settings."},
        )

    try:
        result = await run_chat(message, history)
    except Exception as exc:
        return JSONResponse(status_code=500, content={"detail": f"LLM error: {exc}"})

    return {
        "response": result["response"],
        "tool_calls": result["tool_calls_made"],
        "needs_validation": result["needs_human_validation"],
        "history": result.get("history", []),
    }


@router.post("/chat/validate-exec")
async def validate_exec_endpoint(request: Request):
    """Execute a command in a container after human validation.

    The command is executed on the agent specified by the ``agent`` query
    parameter; the orchestrator never talks to Docker directly.

    If the request body contains ``"type": "clean"``, the endpoint performs
    a ``docker system prune`` on the agent instead of an exec command.
    """
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent = request.query_params.get("agent")
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON body"})

    req_type = data.get("type", "exec")

    if req_type == "clean":
        try:
            result = await agent_manager.clean_agent(agent_name)
            if isinstance(result, dict) and not result.get("success", True):
                return JSONResponse(
                    status_code=500,
                    content={"detail": f"Clean error: {result.get('error', 'unknown')}"},
                )
            return result
        except Exception as exc:
            return JSONResponse(status_code=500, content={"detail": f"Clean error: {exc}"})

    # Default: exec in container
    container_id = data.get("container_id")
    command = data.get("command")
    if not container_id or not command:
        return JSONResponse(
            status_code=400,
            content={"detail": "container_id and command are required"},
        )
    try:
        result = await agent_manager.exec_container(agent_name, container_id, command)
        if isinstance(result, dict) and not result.get("success", True):
            return JSONResponse(
                status_code=500,
                content={"detail": f"Exec error: {result.get('error', 'unknown')}"},
            )
        return result
    except Exception as exc:
        return JSONResponse(status_code=500, content={"detail": f"Exec error: {exc}"})


@router.get("/soul")
async def get_soul_endpoint(request: Request):
    """Read the content of soul.md."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    try:
        content = read_soul()
        return {"content": content}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"detail": f"Error reading soul: {exc}"})


@router.put("/soul")
async def update_soul_endpoint(request: Request):
    """Update soul.md with raw text (Content-Type: text/plain)."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    body = await request.body()
    content = body.decode("utf-8")
    try:
        update_soul(content)
        return {"success": True}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"detail": f"Error updating soul: {exc}"})


@router.websocket("/chat/stream")
async def chat_stream_ws(websocket: WebSocket):
    """WebSocket for streaming the LLM chat response chunk by chunk.

    Receives: {"message": "...", "history": [...]}
    Sends JSON messages of type:
      - "token":       incremental text delta
      - "tool_call":   a tool is about to be executed
      - "tool_result": the result of a tool execution
      - "done":        final response with tool_calls and needs_validation
      - "error":       an error occurred
    """
    username = _check_auth_ws(websocket)
    if username is None:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    try:
        data = await websocket.receive_json()
        message = data.get("message", "")
        history = data.get("history") or []

        if not message:
            await websocket.send_json({"type": "error", "detail": "message is required"})
            await websocket.close()
            return

        llm = LLMClient()
        if not llm.is_configured():
            await websocket.send_json({"type": "error", "detail": "LLM is not configured"})
            await websocket.close()
            return

        # Build the full message list
        system_prompt = await build_system_prompt()
        messages: list = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": message})

        tool_calls_made: list = []
        needs_human_validation: list = []
        max_rounds = 10

        for _round in range(max_rounds):
            accumulated_content = ""
            accumulated_tool_calls: dict = {}  # keyed by tool-call index

            # --- Stream the current round ---
            try:
                async for chunk in llm.chat_stream(messages, tools=TOOLS):
                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}

                    # Text content delta
                    if delta.get("content"):
                        accumulated_content += delta["content"]
                        await websocket.send_json(
                            {"type": "token", "content": delta["content"]}
                        )

                    # Tool call deltas (accumulated incrementally)
                    if delta.get("tool_calls"):
                        for tc in delta["tool_calls"]:
                            idx = tc.get("index", 0)
                            if idx not in accumulated_tool_calls:
                                accumulated_tool_calls[idx] = {
                                    "id": tc.get("id", ""),
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }
                            if tc.get("id"):
                                accumulated_tool_calls[idx]["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                accumulated_tool_calls[idx]["function"]["name"] += fn["name"]
                            if fn.get("arguments"):
                                accumulated_tool_calls[idx]["function"]["arguments"] += fn["arguments"]
            except RuntimeError as exc:
                await websocket.send_json({"type": "error", "detail": str(exc)})
                break

            # --- If tool calls were collected, execute them ---
            if accumulated_tool_calls:
                tool_calls_list = [
                    accumulated_tool_calls[k] for k in sorted(accumulated_tool_calls.keys())
                ]

                # Append the assistant message (with tool_calls) to conversation
                messages.append({
                    "role": "assistant",
                    "content": accumulated_content,
                    "tool_calls": tool_calls_list,
                })

                for tc in tool_calls_list:
                    fn = tc.get("function") or {}
                    tool_name = fn.get("name", "")
                    try:
                        tool_args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        tool_args = {}

                    tool_call_id = tc.get("id", "")
                    tool_calls_made.append({
                        "name": tool_name,
                        "arguments": tool_args,
                        "id": tool_call_id,
                    })

                    await websocket.send_json(
                        {"type": "tool_call", "name": tool_name, "arguments": tool_args}
                    )

                    tool_result = await execute_tool(tool_name, tool_args)

                    if tool_result.startswith(HUMAN_VALIDATION_MARKER):
                        needs_human_validation.append({
                            "name": tool_name,
                            "arguments": tool_args,
                            "id": tool_call_id,
                        })
                        if tool_name == "clean_agent":
                            tool_result_msg = (
                                f"Cette action nécessite une validation humaine avant exécution. "
                                f"Agent: {tool_args.get('agent_name', '')}, "
                                f"action: docker system prune -f. "
                                f"Informe l'utilisateur que le nettoyage est en attente de validation."
                            )
                        else:
                            tool_result_msg = (
                                f"Cette commande nécessite une validation humaine avant exécution. "
                                f"Commande proposée: {tool_args.get('command', '')} "
                                f"sur le container {tool_args.get('container_id', '')}. "
                                f"Informe l'utilisateur que la commande est en attente de validation."
                            )
                    else:
                        tool_result_msg = tool_result

                    await websocket.send_json(
                        {"type": "tool_result", "name": tool_name, "result": tool_result_msg}
                    )

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": tool_result_msg,
                    })

                # Continue the loop for the next LLM round
                continue

            # --- No tool calls: this is the final response ---
            await websocket.send_json({
                "type": "done",
                "response": accumulated_content,
                "tool_calls": tool_calls_made,
                "needs_validation": needs_human_validation,
            })
            break
        else:
            # Round limit reached without a final response
            await websocket.send_json({
                "type": "done",
                "response": "J'ai atteint la limite d'interactions avec les outils.",
                "tool_calls": tool_calls_made,
                "needs_validation": needs_human_validation,
            })

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await websocket.send_json({"type": "error", "detail": str(exc)})
        except Exception:
            pass
    try:
        await websocket.close()
    except Exception:
        pass