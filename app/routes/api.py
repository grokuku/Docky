"""API endpoints for Docky (JSON, JWT-protected).

The orchestrator no longer talks to Docker directly: every Docker-related
operation is delegated to a remote *agent* through ``agent_manager``. Each
request must specify which agent it targets (via the ``agent`` query
parameter or, for POST bodies, the ``agent`` field). The special value
``all`` aggregates data from every configured agent.
"""

import json
from typing import Optional

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import JSONResponse, PlainTextResponse

from app.auth.router import COOKIE_NAME
from app.auth.jwt_utils import verify_token
from app.agent_manager.client import agent_manager
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
# Containers
# ---------------------------------------------------------------------------

@router.get("/containers")
async def api_list_containers(request: Request, agent: str = Query("all")):
    username = _check_auth(request)
    if username is None:
        return _unauthorized()
    if agent == "all":
        return await agent_manager.get_all_containers()
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

    TODO: proxy this WebSocket toward the agent's own
    ``/agent/containers/{id}/logs/stream`` endpoint. Implementing a full
    bidirectional WebSocket proxy is deferred to a later iteration.
    """
    username = _check_auth_ws(websocket)
    if username is None:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    await websocket.send_text(
        "WebSocket proxy to agent not yet implemented. "
        "Use the agent's WebSocket endpoint directly."
    )
    await websocket.close(code=1011)


# ---------------------------------------------------------------------------
# Console (exec)
# ---------------------------------------------------------------------------

@router.websocket("/containers/{container_id}/exec")
async def ws_container_exec(websocket: WebSocket, container_id: str):
    """WebSocket for interactive exec in a container (bidirectional).

    TODO: proxy this WebSocket toward the agent's own
    ``/agent/containers/{id}/exec`` endpoint. Implementing a full
    bidirectional WebSocket proxy is deferred to a later iteration.
    """
    username = _check_auth_ws(websocket)
    if username is None:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    await websocket.send_text(
        "WebSocket proxy to agent not yet implemented. "
        "Use the agent's WebSocket endpoint directly."
    )
    await websocket.close(code=1011)


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
        return await agent_manager.get_all_ports()
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
        return await agent_manager.get_all_stacks()
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
    for c in containers:
        labels = c.get("labels", {}) if isinstance(c, dict) else {}
        stack_label = labels.get("com.docker.compose.project") or c.get("stack")
        if stack_label and stack_label == name:
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
    result = await agent_manager.deploy_stack(agent_name, name)
    err = _check_agent_error(result)
    return err if err is not None else result


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
    """Execute a command in a container after human validation.

    The command is executed on the agent specified by the ``agent`` query
    parameter; the orchestrator never talks to Docker directly.
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