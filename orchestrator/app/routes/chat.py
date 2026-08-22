"""LLM chat endpoints (``/api/chat*``, ``/api/soul``).

Extracted from ``app.routes.api``. ``LLMClient`` and ``agent_manager`` are
resolved through the façade ``app.routes.api`` at call time (``_api()``) so
the tests' monkeypatches of ``app.routes.api.LLMClient`` and
``app.routes.api.agent_manager`` keep taking effect. The remaining ``app.llm``
symbols (``run_chat``, ``read_soul``, ...) are imported directly from
``app.llm.client`` — they are not monkeypatched on the ``app.routes.api``
namespace.
"""

import json

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from app.llm.client import (
    run_chat,
    read_soul,
    update_soul,
    execute_tool,
    build_system_prompt,
    TOOLS,
    HUMAN_VALIDATION_MARKER,
)
from app.routes.api_helpers import (
    _check_auth,
    _check_auth_ws,
    _unauthorized,
    _resolve_agent,
)

router = APIRouter()


def _api():
    """Résolution tardive du namespace app.routes.api (évite tout cycle)."""
    from app.routes import api
    return api


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

    llm = _api().LLMClient()
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
            result = await _api().agent_manager.clean_agent(agent_name)
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
        result = await _api().agent_manager.exec_container(agent_name, container_id, command)
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

        llm = _api().LLMClient()
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
