"""API endpoints for Docky (JSON, JWT-protected)."""

import json
from typing import Optional

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect, Query, Body
from fastapi.responses import JSONResponse, PlainTextResponse

from app.auth.router import COOKIE_NAME
from app.auth.jwt_utils import verify_token
from app.docker_manager import client as docker_client
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


# ---------------------------------------------------------------------------
# Auth helper
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
# Stacks
# ---------------------------------------------------------------------------

@router.get("/stacks")
async def api_list_stacks(request: Request):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()

    stacks = docker_client.list_stacks()
    result = []
    for s in stacks:
        containers = docker_client.get_stack_containers(s["name"])
        status = docker_client.get_stack_status(s["name"])
        ports = docker_client.get_stack_ports(s["name"])
        running = sum(1 for c in containers if c["status"] == "running")
        result.append({
            "name": s["name"],
            "path": s["path"],
            "has_compose": s["has_compose"],
            "has_env": s["has_env"],
            "container_count": len(containers),
            "running_count": running,
            "status": status,
            "ports": ports,
        })
    return result


@router.get("/stacks/{name}/containers")
async def api_stack_containers(request: Request, name: str):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    containers = docker_client.get_stack_containers(name)
    return containers


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------

@router.get("/containers")
async def api_list_containers(request: Request):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    return docker_client.list_containers(all=True)


@router.get("/containers/{container_id}")
async def api_get_container(request: Request, container_id: str):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    c = docker_client.get_container(container_id)
    if c is None:
        return JSONResponse(status_code=404, content={"detail": "Container not found"})
    # Attach stats
    c["stats"] = docker_client.get_container_stats(container_id)
    return c


# ---------------------------------------------------------------------------
# Actions - Containers
# ---------------------------------------------------------------------------

@router.post("/containers/{container_id}/start")
async def api_start_container(request: Request, container_id: str):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    ok = docker_client.start_container(container_id)
    return {"success": ok}


@router.post("/containers/{container_id}/stop")
async def api_stop_container(request: Request, container_id: str):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    ok = docker_client.stop_container(container_id)
    return {"success": ok}


@router.post("/containers/{container_id}/restart")
async def api_restart_container(request: Request, container_id: str):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    ok = docker_client.restart_container(container_id)
    return {"success": ok}


# ---------------------------------------------------------------------------
# Actions - Stacks
# ---------------------------------------------------------------------------

@router.post("/stacks/{name}/start")
async def api_stack_start(request: Request, name: str):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    result = docker_client.compose_up(name)
    return result


@router.post("/stacks/{name}/stop")
async def api_stack_stop(request: Request, name: str):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    result = docker_client.compose_stop(name)
    return result


@router.post("/stacks/{name}/restart")
async def api_stack_restart(request: Request, name: str):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    result = docker_client.compose_restart(name)
    return result


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

@router.get("/containers/{container_id}/logs")
async def api_container_logs(request: Request, container_id: str, tail: int = Query(100)):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    lines = docker_client.get_container_logs(container_id, tail=tail)
    return {"lines": lines}


@router.websocket("/containers/{container_id}/logs/stream")
async def ws_container_logs(websocket: WebSocket, container_id: str):
    """WebSocket for streaming container logs in real-time."""
    username = _check_auth_ws(websocket)
    if username is None:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    try:
        # Stream existing + new logs
        import asyncio
        for line in docker_client.get_container_logs_stream(container_id, tail=100):
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


# ---------------------------------------------------------------------------
# Console (exec)
# ---------------------------------------------------------------------------

@router.websocket("/containers/{container_id}/exec")
async def ws_container_exec(websocket: WebSocket, container_id: str):
    """WebSocket for interactive exec in a container (bidirectional)."""
    username = _check_auth_ws(websocket)
    if username is None:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    try:
        # Wait for commands from the client and execute them
        while True:
            command = await websocket.receive_text()
            if not command.strip():
                continue
            # Execute one-shot command
            output = docker_client.exec_in_container(container_id, command, tty=False)
            await websocket.send_text(output)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_text(f"[error] {e}")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

@router.get("/containers/{container_id}/stats")
async def api_container_stats(request: Request, container_id: str):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    stats = docker_client.get_container_stats(container_id)
    return stats


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------

@router.get("/ports")
async def api_get_ports(request: Request):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    return docker_client.get_used_ports()


# ---------------------------------------------------------------------------
# Update check
# ---------------------------------------------------------------------------

@router.get("/containers/{container_id}/update-check")
async def api_update_check(request: Request, container_id: str):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    result = docker_client.check_image_update(container_id)
    return result


# ---------------------------------------------------------------------------
# Stack files (Phase 3 - Editor)
# ---------------------------------------------------------------------------

def _safe_call(func):
    """Helper to run a docker_client function and convert exceptions to 4xx."""
    try:
        return func()
    except (FileNotFoundError, FileExistsError):
        return JSONResponse(status_code=404, content={"detail": str(func)})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})


@router.get("/stacks/{name}/files")
async def api_list_stack_files(request: Request, name: str):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    try:
        files = docker_client.get_stack_files(name)
        return {"files": files}
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"detail": f"Stack '{name}' not found"})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})


@router.put("/stacks/{name}/files/{filename}/permissions")
async def api_set_file_permissions(request: Request, name: str, filename: str):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON body"})
    mode = data.get("mode")
    if mode is None:
        return JSONResponse(status_code=400, content={"detail": "mode is required"})
    try:
        result = docker_client.set_file_permissions(name, filename, mode)
        return result
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"detail": "File not found"})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})


@router.get("/stacks/{name}/files/{filename:path}")
async def api_get_stack_file(request: Request, name: str, filename: str):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    try:
        content = docker_client.get_stack_file(name, filename)
        return PlainTextResponse(content)
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"detail": "File not found"})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})


@router.put("/stacks/{name}/files/{filename:path}")
async def api_put_stack_file(request: Request, name: str, filename: str):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    body = await request.body()
    content = body.decode("utf-8")
    try:
        docker_client.save_stack_file(name, filename, content)
        return {"success": True, "name": filename}
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"detail": "Stack not found"})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})


@router.get("/stacks/{name}/compose")
async def api_get_compose(request: Request, name: str):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    try:
        content = docker_client.get_stack_file(name, "docker-compose.yml")
        return PlainTextResponse(content)
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"detail": "Compose file not found"})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})


@router.put("/stacks/{name}/compose")
async def api_put_compose(request: Request, name: str):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    body = await request.body()
    content = body.decode("utf-8")
    try:
        docker_client.save_stack_file(name, "docker-compose.yml", content)
        return {"success": True}
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"detail": "Stack not found"})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})


@router.get("/stacks/{name}/env")
async def api_get_env(request: Request, name: str):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    try:
        content = docker_client.get_stack_file(name, ".env")
        return PlainTextResponse(content)
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"detail": ".env file not found"})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})


@router.put("/stacks/{name}/env")
async def api_put_env(request: Request, name: str):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    body = await request.body()
    content = body.decode("utf-8")
    try:
        docker_client.save_stack_file(name, ".env", content)
        return {"success": True}
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"detail": "Stack not found"})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})


@router.post("/stacks")
async def api_create_stack(request: Request):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON body"})
    name = data.get("name", "")
    compose = data.get("compose", "")
    env = data.get("env", "")
    if not name:
        return JSONResponse(status_code=400, content={"detail": "name is required"})
    try:
        result = docker_client.create_stack(name, compose, env)
        return result
    except FileExistsError:
        return JSONResponse(status_code=409, content={"detail": "Stack already exists"})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})


@router.delete("/stacks/{name}")
async def api_delete_stack(request: Request, name: str):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    try:
        result = docker_client.delete_stack(name)
        return result
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"detail": "Stack not found"})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})


@router.post("/stacks/{name}/deploy")
async def api_deploy_stack(request: Request, name: str):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    try:
        result = docker_client.deploy_stack(name)
        return result
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"detail": "Stack not found"})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})


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
    }


@router.post("/chat/validate-exec")
async def validate_exec_endpoint(request: Request):
    """Execute a command in a container after human validation."""
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON body"})
    container_id = data.get("container_id")
    command = data.get("command")
    if not container_id or not command:
        return JSONResponse(
            status_code=400,
            content={"detail": "container_id and command are required"},
        )
    try:
        output = docker_client.exec_in_container(container_id, command, tty=False)
        return {"success": True, "output": output}
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
        system_prompt = build_system_prompt()
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