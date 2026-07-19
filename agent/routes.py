"""All REST and WebSocket endpoints for the Docky Agent service.

Every endpoint (except ``/agent/health``) is protected by API key
authentication via the ``Authorization: Bearer <key>`` header.
"""

import asyncio

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import JSONResponse, PlainTextResponse

from agent import docker_manager
from agent.auth import require_api_key, verify_api_key_ws

router = APIRouter(prefix="/agent")


# ---------------------------------------------------------------------------
# Health (no auth)
# ---------------------------------------------------------------------------

@router.get("/health")
async def health():
    """Lightweight health-check endpoint for the orchestrator to ping."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------

@router.get("/containers")
async def list_containers(request: Request):
    auth_err = require_api_key(request)
    if auth_err:
        return auth_err
    return await asyncio.to_thread(docker_manager.list_containers, all=True)


@router.get("/containers/{container_id}")
async def get_container(request: Request, container_id: str):
    auth_err = require_api_key(request)
    if auth_err:
        return auth_err
    c = await asyncio.to_thread(docker_manager.get_container, container_id)
    if c is None:
        return JSONResponse(status_code=404, content={"error": "Container not found"})
    c["stats"] = await asyncio.to_thread(docker_manager.get_container_stats, container_id)
    return c


@router.get("/containers/{container_id}/stats")
async def get_container_stats(request: Request, container_id: str):
    auth_err = require_api_key(request)
    if auth_err:
        return auth_err
    return await asyncio.to_thread(docker_manager.get_container_stats, container_id)


@router.get("/containers/{container_id}/logs")
async def get_container_logs(request: Request, container_id: str, tail: int = Query(100)):
    auth_err = require_api_key(request)
    if auth_err:
        return auth_err
    lines = await asyncio.to_thread(docker_manager.get_container_logs, container_id, tail=tail)
    return {"lines": lines}


@router.websocket("/containers/{container_id}/logs/stream")
async def stream_container_logs(websocket: WebSocket, container_id: str):
    """WebSocket for streaming container logs in real-time.

    Auth is via the ``api_key`` query parameter.
    """
    if not await verify_api_key_ws(websocket):
        await websocket.close(code=4401)
        return

    await websocket.accept()
    try:
        for line in docker_manager.get_container_logs_stream(container_id, tail=100):
            await websocket.send_text(line)
            await asyncio.sleep(0.01)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_text(f"[error] {e}")
        except Exception:
            pass
    try:
        await websocket.close()
    except Exception:
        pass


@router.websocket("/containers/{container_id}/exec")
async def exec_in_container(websocket: WebSocket, container_id: str):
    """WebSocket for interactive exec in a container (bidirectional).

    The client sends text commands; the agent executes them one-shot and
    returns the output.  Auth is via the ``api_key`` query parameter.
    """
    if not await verify_api_key_ws(websocket):
        await websocket.close(code=4401)
        return

    await websocket.accept()
    try:
        while True:
            command = await websocket.receive_text()
            if not command.strip():
                continue
            output = docker_manager.exec_in_container(container_id, command, tty=False)
            await websocket.send_text(output)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_text(f"[error] {e}")
        except Exception:
            pass


@router.post("/containers/{container_id}/exec")
async def exec_one_shot(request: Request, container_id: str):
    """Execute a one-shot command in a container (non-interactive).

    Unlike the WebSocket ``/exec`` endpoint, this is a simple request/response
    call used by the orchestrator for the ``validate-exec`` flow.
    """
    auth_err = require_api_key(request)
    if auth_err:
        return auth_err
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})
    command = data.get("command", "")
    if not command.strip():
        return JSONResponse(status_code=400, content={"error": "command is required"})
    try:
        output = await asyncio.to_thread(docker_manager.exec_in_container, container_id, command, tty=False)
        return {"success": True, "output": output}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/containers/{container_id}/start")
async def start_container(request: Request, container_id: str):
    auth_err = require_api_key(request)
    if auth_err:
        return auth_err
    ok = await asyncio.to_thread(docker_manager.start_container, container_id)
    return {"success": ok}


@router.post("/containers/{container_id}/stop")
async def stop_container(request: Request, container_id: str):
    auth_err = require_api_key(request)
    if auth_err:
        return auth_err
    ok = await asyncio.to_thread(docker_manager.stop_container, container_id)
    return {"success": ok}


@router.post("/containers/{container_id}/restart")
async def restart_container(request: Request, container_id: str):
    auth_err = require_api_key(request)
    if auth_err:
        return auth_err
    ok = await asyncio.to_thread(docker_manager.restart_container, container_id)
    return {"success": ok}


@router.get("/containers/{container_id}/update-check")
async def update_check(request: Request, container_id: str):
    auth_err = require_api_key(request)
    if auth_err:
        return auth_err
    return await asyncio.to_thread(docker_manager.check_image_update, container_id)


# ---------------------------------------------------------------------------
# Stacks
# ---------------------------------------------------------------------------

@router.get("/stacks")
async def list_stacks(request: Request):
    auth_err = require_api_key(request)
    if auth_err:
        return auth_err

    # Récupérer stacks et containers en parallèle (2 appels au lieu de 1+3N)
    stacks, all_containers = await asyncio.gather(
        asyncio.to_thread(docker_manager.list_stacks),
        asyncio.to_thread(docker_manager.list_containers, all=True),
    )

    # Calculer les infos localement sans rappeler Docker
    result = []
    for s in stacks:
        name = s["name"]
        # Filtrer les containers de cette stack
        if name == "Standalone":
            containers = [c for c in all_containers if not c.get("stack")]
        else:
            containers = [c for c in all_containers if c.get("stack") == name]

        running = sum(1 for c in containers if c.get("status") == "running")

        if not containers:
            status = "empty"
        elif running == len(containers):
            status = "running"
        elif running == 0:
            status = "stopped"
        else:
            status = "partial"

        # Calculer les ports localement
        ports_set = set()
        for c in containers:
            for p in c.get("ports", []):
                hp = p.get("host_port", "")
                if hp:
                    ports_set.add(hp)
        ports = sorted(ports_set, key=lambda x: int(x) if x.isdigit() else 0)

        result.append({
            "name": s["name"],
            "path": s.get("path", ""),
            "has_compose": s.get("has_compose", False),
            "has_env": s.get("has_env", False),
            "managed": s.get("managed", True),
            "standalone": s.get("standalone", False),
            "container_count": len(containers),
            "running_count": running,
            "status": status,
            "ports": ports,
        })
    return result


@router.post("/stacks")
async def create_stack(request: Request):
    auth_err = require_api_key(request)
    if auth_err:
        return auth_err
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})
    name = data.get("name", "")
    compose = data.get("compose", "")
    env = data.get("env", "")
    if not name:
        return JSONResponse(status_code=400, content={"error": "name is required"})
    try:
        result = docker_manager.create_stack(name, compose, env)
        return result
    except FileExistsError:
        return JSONResponse(status_code=409, content={"error": "Stack already exists"})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.delete("/stacks/{name}")
async def delete_stack(request: Request, name: str):
    auth_err = require_api_key(request)
    if auth_err:
        return auth_err
    try:
        result = docker_manager.delete_stack(name)
        return result
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"error": "Stack not found"})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.post("/stacks/{name}/deploy")
async def deploy_stack(request: Request, name: str):
    auth_err = require_api_key(request)
    if auth_err:
        return auth_err
    try:
        result = await docker_manager.deploy_stack(name)
        return result
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"error": "Stack not found"})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.post("/stacks/{name}/start")
async def start_stack(request: Request, name: str):
    auth_err = require_api_key(request)
    if auth_err:
        return auth_err
    return await docker_manager.compose_up(name)


@router.post("/stacks/{name}/stop")
async def stop_stack(request: Request, name: str):
    auth_err = require_api_key(request)
    if auth_err:
        return auth_err
    return await docker_manager.compose_stop(name)


@router.post("/stacks/{name}/restart")
async def restart_stack(request: Request, name: str):
    auth_err = require_api_key(request)
    if auth_err:
        return auth_err
    return await docker_manager.compose_restart(name)


@router.post("/stacks/{name}/update")
async def update_stack(request: Request, name: str):
    auth_err = require_api_key(request)
    if auth_err:
        return auth_err
    try:
        result = await docker_manager.update_stack(name)
        return result
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"error": "Stack not found"})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ---------------------------------------------------------------------------
# Stack files
# ---------------------------------------------------------------------------

@router.get("/stacks/{name}/files")
async def list_stack_files(request: Request, name: str):
    auth_err = require_api_key(request)
    if auth_err:
        return auth_err
    try:
        files = docker_manager.get_stack_files(name)
        return {"files": files}
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"error": f"Stack '{name}' not found"})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.get("/stacks/{name}/files/{filename}")
async def get_stack_file(request: Request, name: str, filename: str):
    auth_err = require_api_key(request)
    if auth_err:
        return auth_err
    try:
        content = docker_manager.get_stack_file(name, filename)
        return PlainTextResponse(content)
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"error": "File not found"})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.put("/stacks/{name}/files/{filename}")
async def save_stack_file(request: Request, name: str, filename: str):
    auth_err = require_api_key(request)
    if auth_err:
        return auth_err
    body = await request.body()
    content = body.decode("utf-8")
    try:
        docker_manager.save_stack_file(name, filename, content)
        return {"success": True, "name": filename}
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"error": "Stack not found"})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.put("/stacks/{name}/files/{filename}/permissions")
async def set_file_permissions(request: Request, name: str, filename: str):
    auth_err = require_api_key(request)
    if auth_err:
        return auth_err
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})
    mode = data.get("mode")
    if mode is None:
        return JSONResponse(status_code=400, content={"error": "mode is required"})
    try:
        result = docker_manager.set_file_permissions(name, filename, mode)
        return result
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"error": "File not found"})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------

@router.get("/ports")
async def get_ports(request: Request):
    auth_err = require_api_key(request)
    if auth_err:
        return auth_err
    return await asyncio.to_thread(docker_manager.get_used_ports)


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------

@router.post("/system/prune")
async def system_prune(request: Request):
    auth_err = require_api_key(request)
    if auth_err:
        return auth_err
    try:
        result = await docker_manager.system_prune()
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}