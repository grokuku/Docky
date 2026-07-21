"""All REST and WebSocket endpoints for the Docky Agent service.

Every endpoint (except ``/agent/health``) is protected by API key
authentication via the ``Authorization: Bearer <key>`` header.
"""

import asyncio
import json
import logging
import re
import threading

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import JSONResponse, PlainTextResponse

from agent import docker_manager
from agent.auth import require_api_key, verify_api_key_ws

logger = logging.getLogger(__name__)

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
    """WebSocket for interactive PTY exec in a container (bidirectional).

    Creates an interactive shell (bash) inside the container and connects it
    to the WebSocket via a Docker exec PTY.  Supports resize events sent as
    ``{"type":"resize","cols":X,"rows":Y}`` JSON messages.

    Auth is via the ``api_key`` query parameter (same as ``logs/stream``).
    """
    if not await verify_api_key_ws(websocket):
        await websocket.close(code=4401)
        return

    await websocket.accept()

    # Create interactive exec instance
    try:
        sock, exec_id, raw_sock = await asyncio.to_thread(
            docker_manager.exec_interactive_start, container_id
        )
    except Exception as e:
        logger.exception("Failed to start interactive exec in container %s", container_id)
        await websocket.send_text(f"Error: {e}")
        await websocket.close()
        return

    loop = asyncio.get_running_loop()

    async def read_docker():
        """Read from Docker socket and send to WebSocket."""
        try:
            while True:
                data = await loop.sock_recv(raw_sock, 4096)
                if not data:
                    break
                await websocket.send_bytes(data)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("read_docker ended: %s", e)

    async def read_websocket():
        """Read from WebSocket and send to Docker socket.

        Detects resize control messages (JSON like ``{"type":"resize",...}``)
        and handles them by calling ``exec_resize`` instead of forwarding
        them to the container's stdin.
        """
        try:
            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    break

                if msg["type"] == "websocket.receive":
                    # Normalise to a string: handle both text and bytes frames
                    raw = None
                    if isinstance(msg.get("text"), str):
                        raw = msg["text"]
                    elif isinstance(msg.get("bytes"), bytes):
                        raw = msg["bytes"].decode('utf-8', errors='replace')

                    if raw is not None:
                        # --- Detect resize control messages ---
                        is_resize = False

                        # 1. Try standard JSON parsing
                        try:
                            cmd = json.loads(raw)
                            if isinstance(cmd, dict) and cmd.get("type") == "resize":
                                rows = int(cmd.get("rows", 24))
                                cols = int(cmd.get("cols", 80))
                                await asyncio.to_thread(
                                    docker_manager.exec_resize,
                                    container_id, exec_id, rows, cols
                                )
                                is_resize = True
                        except (json.JSONDecodeError, TypeError, ValueError):
                            pass

                        # 2. Fallback: detect non-standard JSON like
                        #    {type:resize,cols:111,rows:39} (unquoted keys)
                        if not is_resize:
                            m = re.match(
                                r'^\s*\{\s*"?type"?\s*:\s*"?resize"?\s*,',
                                raw, re.IGNORECASE
                            )
                            if m:
                                cols_match = re.search(r'"?cols"?\s*:\s*(\d+)', raw)
                                rows_match = re.search(r'"?rows"?\s*:\s*(\d+)', raw)
                                cols = int(cols_match.group(1)) if cols_match else 80
                                rows = int(rows_match.group(1)) if rows_match else 24
                                await asyncio.to_thread(
                                    docker_manager.exec_resize,
                                    container_id, exec_id, rows, cols
                                )
                                is_resize = True

                        if is_resize:
                            continue

                        # --- Forward user input to the container's PTY ---
                        payload = raw.encode() if isinstance(raw, str) else raw
                        await loop.sock_sendall(raw_sock, payload)
        except asyncio.CancelledError:
            raise
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.debug("read_websocket ended: %s", e)

    try:
        # Use FIRST_COMPLETED so that when either side closes (e.g. the
        # user types ``exit`` or the client disconnects), we cancel the
        # other task immediately instead of hanging forever.
        tasks = [
            asyncio.create_task(read_docker()),
            asyncio.create_task(read_websocket()),
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
    finally:
        try:
            raw_sock.close()
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass
        try:
            await websocket.close()
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


@router.get("/containers/{container_id}/edit-spec")
async def get_container_spec_for_edit(request: Request, container_id: str):
    auth_err = require_api_key(request)
    if auth_err:
        return auth_err
    spec = await asyncio.to_thread(docker_manager._get_container_full_spec, container_id)
    if spec is None:
        return JSONResponse(status_code=404, content={"error": "Container not found"})
    return spec


@router.post("/containers/{container_id}/update")
async def update_container_route(request: Request, container_id: str):
    auth_err = require_api_key(request)
    if auth_err:
        return auth_err
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})
    result = await docker_manager.update_container(container_id, data)
    return result


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


@router.post("/stacks/import")
async def import_stack_endpoint(request: Request):
    auth_error = require_api_key(request)
    if auth_error:
        return auth_error
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})
    source_path = data.get("source_path", "")
    stack_name = data.get("stack_name")
    dry_run = data.get("dry_run", False)
    if not source_path:
        return JSONResponse(status_code=400, content={"error": "source_path is required"})
    try:
        result = await asyncio.to_thread(docker_manager.import_stack, source_path, stack_name, dry_run)
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.delete("/stacks/{name}")
async def delete_stack(request: Request, name: str):
    auth_err = require_api_key(request)
    if auth_err:
        return auth_err
    try:
        result = await docker_manager.delete_stack(name)
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
    return await docker_manager.compose_start(name)


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


@router.get("/stacks/{stack_name}/files-with-content")
async def list_stack_files_with_content(request: Request, stack_name: str):
    """List all files in a stack WITH their content in a single call.

    Returns a JSON object:
    .. code-block:: json

        {"files": [{"filename": "docker-compose.yml", "content": "..."}, ...]}
    """
    auth_error = require_api_key(request)
    if auth_error:
        return auth_error
    try:
        files = await asyncio.to_thread(docker_manager.get_stack_files, stack_name)
        result = []
        for f in files:
            try:
                content = await asyncio.to_thread(docker_manager.get_stack_file, stack_name, f["name"])
                result.append({"filename": f["name"], "content": content, "size": f.get("size", 0)})
            except Exception:
                result.append({"filename": f["name"], "content": None, "size": f.get("size", 0)})
        return {"files": result}
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"error": f"Stack '{stack_name}' not found"})
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
# Stack git history
# ---------------------------------------------------------------------------


@router.get("/stacks/{name}/history")
async def get_stack_history(request: Request, name: str):
    auth_err = require_api_key(request)
    if auth_err:
        return auth_err
    try:
        history = docker_manager._get_git_history(name)
        return {"history": history}
    except Exception as e:
        return {"history": [], "error": str(e)}


@router.get("/stacks/{name}/history/{hash}")
async def get_stack_version(request: Request, name: str, hash: str):
    auth_err = require_api_key(request)
    if auth_err:
        return auth_err
    version = docker_manager._get_git_version(name, hash)
    if version is None:
        return JSONResponse(status_code=404, content={"error": "Version not found"})
    return version


@router.post("/stacks/{name}/history/restore/{hash}")
async def restore_stack_version(request: Request, name: str, hash: str):
    auth_err = require_api_key(request)
    if auth_err:
        return auth_err
    result = docker_manager._git_restore(name, hash)
    return result


@router.get("/settings/git-history")
async def get_git_history_settings(request: Request):
    auth_err = require_api_key(request)
    if auth_err:
        return auth_err
    return docker_manager.get_history_settings()


@router.put("/settings/git-history")
async def update_git_history_settings(request: Request):
    auth_err = require_api_key(request)
    if auth_err:
        return auth_err
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})
    max_versions = data.get("max_versions", 50)
    docker_manager.set_history_settings(max_versions)
    return {"success": True}


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


# ---------------------------------------------------------------------------
# Docker Events (WebSocket)
# ---------------------------------------------------------------------------


@router.websocket("/events")
async def stream_docker_events(websocket: WebSocket):
    """Stream Docker events to the orchestrator via WebSocket.

    Uses a daemon thread to bridge the synchronous Docker events generator
    to the async WebSocket loop via an ``asyncio.Queue``.
    """
    if not await verify_api_key_ws(websocket):
        await websocket.close(code=4401)
        return

    await websocket.accept()

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)

    def _producer():
        """Run in a daemon thread: push Docker events into the queue."""
        try:
            for event in docker_manager.watch_docker_events():
                if isinstance(event, dict):
                    fut = asyncio.run_coroutine_threadsafe(
                        queue.put(event), loop
                    )
                    fut.result(timeout=0.1)
        except Exception:
            asyncio.run_coroutine_threadsafe(
                queue.put(None), loop
            ).result()

    thread = threading.Thread(target=_producer, daemon=True)
    thread.start()

    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass