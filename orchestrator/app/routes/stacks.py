"""Stack endpoints (``/api/stacks*``).

Extracted from ``app.routes.api``. ``agent_manager`` is resolved through the
façade ``app.routes.api`` at call time (``_api()``) so the tests' monkeypatch
of ``app.routes.api.agent_manager`` keeps taking effect.
"""

import logging

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse, PlainTextResponse

from app.routes.api_helpers import (
    _check_auth,
    _unauthorized,
    _resolve_agent,
    _check_agent_error,
    _sse_action_response,
)

router = APIRouter()


def _api():
    """Résolution tardive du namespace app.routes.api (évite tout cycle)."""
    from app.routes import api
    return api


@router.get("/stacks")
async def api_list_stacks(request: Request, agent: str = Query("all")):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_manager = _api().agent_manager
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
    containers = await _api().agent_manager.get_containers(agent_name)
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
    """Start a stack (``docker compose up -d``) — streamed as SSE progress lines."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    return _sse_action_response(agent_name, _api().agent_manager.stream_start_stack(agent_name, name))


@router.post("/stacks/{name}/stop")
async def api_stack_stop(request: Request, name: str, agent: str = Query(...)):
    """Stop a stack — streamed as SSE progress lines."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    return _sse_action_response(agent_name, _api().agent_manager.stream_stop_stack(agent_name, name))


@router.post("/stacks/{name}/restart")
async def api_stack_restart(request: Request, name: str, agent: str = Query(...)):
    """Restart a stack — streamed as SSE progress lines."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    return _sse_action_response(agent_name, _api().agent_manager.stream_restart_stack(agent_name, name))


@router.post("/stacks/{name}/update")
async def api_stack_update(request: Request, name: str, agent: str = Query(...)):
    """Update a stack (pull + up -d) — streamed as SSE progress lines."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    return _sse_action_response(agent_name, _api().agent_manager.stream_update_stack(agent_name, name))


@router.get("/stacks/{name}/update-check")
async def api_stack_update_check(
    request: Request, name: str, agent: str = Query(...)
):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    return await _api().agent_manager.check_stack_update(agent_name, name)


@router.get("/stacks/{name}/logs")
async def api_stack_logs(
    request: Request, name: str, tail: int = Query(100), agent: str = Query(...)
):
    """Return the last ``tail`` log lines for a stack (docker compose logs).

    Returns the same ``{"lines": [{message, stream}, ...]}`` shape as the
    container logs endpoint so the popup ``logs.html`` can reuse its rendering.
    """
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    result = await _api().agent_manager.get_stack_logs(agent_name, name, tail)
    if not isinstance(result, dict) or not result.get("success"):
        message = result.get("error", "Failed to fetch stack logs") if isinstance(result, dict) else "Failed to fetch stack logs"
        return JSONResponse(status_code=500, content={"detail": message, "error": message})
    output = result.get("output", "") or ""
    lines = [
        {"message": line, "stream": "stdout"}
        for line in output.splitlines()
        if line.strip()
    ]
    return {"lines": lines}


# ---------------------------------------------------------------------------
# Stack files (editor)
# ---------------------------------------------------------------------------

@router.get("/stacks/{name}/files")
async def api_list_stack_files(request: Request, name: str, agent: str = Query(...), include_hidden: bool = Query(False)):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    files = await _api().agent_manager.get_stack_files(agent_name, name, include_hidden=include_hidden)
    return {"files": files}


@router.get("/stacks/{name}/files-with-content")
async def api_list_stack_files_with_content(
    request: Request, name: str, agent: str = Query(...), include_hidden: bool = Query(False)
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
        result = await _api().agent_manager.get_stack_files_with_content(agent_name, name, include_hidden=include_hidden)
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
    content = await _api().agent_manager.get_stack_file(agent_name, name, filename)
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
    result = await _api().agent_manager.save_stack_file(agent_name, name, filename, content)
    err = _check_agent_error(result)
    if err is not None:
        return err
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
    result = await _api().agent_manager.set_permissions(agent_name, name, filename, mode)
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
    content = await _api().agent_manager.get_stack_file(agent_name, name, "docker-compose.yml")
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
    result = await _api().agent_manager.save_stack_file(agent_name, name, "docker-compose.yml", content)
    err = _check_agent_error(result)
    if err is not None:
        return err
    return {"success": True}


@router.get("/stacks/{name}/env")
async def api_get_env(request: Request, name: str, agent: str = Query(...)):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    content = await _api().agent_manager.get_stack_file(agent_name, name, ".env")
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
    result = await _api().agent_manager.save_stack_file(agent_name, name, ".env", content)
    err = _check_agent_error(result)
    if err is not None:
        return err
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
    result = await _api().agent_manager.create_stack(agent_name, name, compose, env)
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
        agent_manager = _api().agent_manager
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
    result = await _api().agent_manager.delete_stack(agent_name, name)
    err = _check_agent_error(result)
    return err if err is not None else result


@router.post("/stacks/{name}/deploy")
async def api_deploy_stack(request: Request, name: str, agent: str = Query(...)):
    """Deploy (down + up) a stack — streamed as SSE progress lines."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err is not None:
        return err
    return _sse_action_response(agent_name, _api().agent_manager.stream_deploy_stack(agent_name, name))


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
    return await _api().agent_manager.get_stack_history(agent_name, name)


@router.get("/stacks/{name}/history/{hash}")
async def api_stack_version(request: Request, name: str, hash: str, agent: str = Query(...)):
    """Return a specific git version for a stack."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err:
        return err
    return await _api().agent_manager.get_stack_version(agent_name, name, hash)


@router.post("/stacks/{name}/history/restore/{hash}")
async def api_restore_stack(request: Request, name: str, hash: str, agent: str = Query(...)):
    """Restore a stack to a specific git version."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    agent_name, err = _resolve_agent(agent)
    if err:
        return err
    return await _api().agent_manager.restore_stack_version(agent_name, name, hash)
