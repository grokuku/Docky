"""Agents management endpoints (``/api/agents``).

Extracted from ``app.routes.api``. ``agent_manager`` is resolved through the
façade ``app.routes.api`` at call time (``_api()``) so the tests' monkeypatch
of ``app.routes.api.agent_manager`` keeps taking effect.
"""

from fastapi import APIRouter, Request

from app.routes.api_helpers import _check_auth, _unauthorized, _resolve_agent

router = APIRouter()


def _api():
    """Résolution tardive du namespace app.routes.api (évite tout cycle)."""
    from app.routes import api
    return api


@router.get("/agents")
async def api_list_agents(request: Request):
    """List all configured agents with their current status."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_manager = _api().agent_manager
    await agent_manager.ping_all()
    return agent_manager.list_agents()


@router.post("/agents/refresh")
async def api_refresh_agents(request: Request):
    """Force a status refresh (ping) of all agents."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_manager = _api().agent_manager
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
    return await _api().agent_manager.get_containers(agent_name)


@router.get("/agents/{name}/stacks")
async def api_agent_stacks(request: Request, name: str):
    """List stacks belonging to a specific agent."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(name)
    if err is not None:
        return err
    return await _api().agent_manager.get_stacks(agent_name)


@router.get("/agents/{name}/ports")
async def api_agent_ports(request: Request, name: str):
    """List ports in use on a specific agent host."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(name)
    if err is not None:
        return err
    return await _api().agent_manager.get_ports(agent_name)
