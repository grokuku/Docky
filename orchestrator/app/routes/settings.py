"""Settings endpoints (``/api/settings/*``) and version (``/api/version*``).

Extracted from ``app.routes.api``. ``agent_manager`` / ``LLMClient`` are
resolved through the façade ``app.routes.api`` at call time (``_api()``) so
the tests' monkeypatches of ``app.routes.api.agent_manager`` and
``app.routes.api.LLMClient`` keep taking effect.
"""

import asyncio
import logging

import httpx
import bcrypt
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import load_settings, save_settings, load_users, save_users
from app.routes.api_helpers import (
    _check_auth,
    _unauthorized,
    _mask_api_key,
)
from app.version import get_version

logger = logging.getLogger(__name__)

router = APIRouter()


def _api():
    """Résolution tardive du namespace app.routes.api (évite tout cycle)."""
    from app.routes import api
    return api


def _coerce_tls_verify(value) -> bool:
    """Normalise a ``tls_verify`` setting into a safe bool (default True)."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _save_agents(agents: list):
    """Persist the agents list into settings.yaml."""
    settings = load_settings()
    settings["agents"] = agents
    save_settings(settings)


# ---------------------------------------------------------------------------
# Settings - LLM configuration
# ---------------------------------------------------------------------------

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
    llm = _api().LLMClient()
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

@router.get("/settings/agents")
async def api_get_settings_agents(request: Request):
    """List configured agents (name, url, masked api_key, status)."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_manager = _api().agent_manager
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
    tls_verify = _coerce_tls_verify(data.get("tls_verify", True))
    agents.append({
        "name": name,
        "url": url,
        "api_key": api_key,
        "tls_verify": tls_verify,
        "ca_cert": data.get("ca_cert") or "",
        "path_mappings": data.get("path_mappings", []) or [],
    })
    settings["agents"] = agents
    save_settings(settings)
    _api().agent_manager.reload()
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
    found["tls_verify"] = _coerce_tls_verify(data.get("tls_verify", found.get("tls_verify", True)))
    found["ca_cert"] = data.get("ca_cert", found.get("ca_cert", "")) or ""
    found["path_mappings"] = data.get("path_mappings", found.get("path_mappings", []) or [])
    save_settings(settings)
    _api().agent_manager.reload()
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
    _api().agent_manager.reload()
    return {"success": True}


@router.post("/settings/agents/{name}/test")
async def api_test_settings_agent(request: Request, name: str):
    """Ping an agent to verify the connection."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_manager = _api().agent_manager
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
    """Return the current Docky version.

    Resolved by ``app.version.get_version`` (``DOCKY_VERSION`` env →
    ``version.txt`` → ``"0.0.0"``), i.e. the same value as the repository's
    ``version.txt``.
    """
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    return {"version": get_version()}


@router.get("/version-check")
async def api_version_check(request: Request):
    """Compare orchestrator version with each agent's version.

    Returns the orchestrator version, each agent's version, and a list of
    mismatches (agents whose version differs from the orchestrator).
    """
    username = _check_auth(request)
    if username is None:
        return _unauthorized()

    agent_manager = _api().agent_manager

    # Read orchestrator version (DOCKY_VERSION env → version.txt → "0.0.0")
    orch_version = get_version()

    # Fetch versions from all agents
    agent_versions = {}
    for name in agent_manager.agents:
        try:
            health = await agent_manager._request(name, "GET", "/agent/health")
            if isinstance(health, dict):
                agent_versions[name] = health.get("version", "unknown")
        except Exception as exc:
            logger.warning("Agent health check failed for '%s': %s", name, exc)
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

    settings = load_settings()
    try:
        max_versions = int(data.get('max_versions', 50))
        if max_versions <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return JSONResponse(
            status_code=400,
            content={"detail": "max_versions must be a positive integer"},
        )

    settings['history_retention'] = {'max_versions': max_versions}
    save_settings(settings)
    # Propagate the new retention value to every configured agent so the
    # agent-side git cleanup uses the same setting.
    await asyncio.gather(*[
        _api().agent_manager.update_git_history_settings(name, {'max_versions': max_versions})
        for name in _api().agent_manager.agents
    ], return_exceptions=True)
    return {"success": True}
