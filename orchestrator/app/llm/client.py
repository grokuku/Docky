"""LLM client and tool-calling system for Docky (façade).

This module is the **façade** of the ``app.llm`` sub-package: it keeps the
``LLMClient`` HTTP layer, the agentic ``run_chat`` loop, and re-exports every
public symbol from the cohesive sub-modules so existing imports (routes, tests)
keep working unchanged:

- ``app.llm.constants`` — shared constants (``HUMAN_VALIDATION_MARKER``,
  ``MAX_TOOL_ROUNDS``, ``_DEFAULT_WEB_ENDPOINT``, ``_TOOLS_DOCKER_AGENT_PARAM``).
- ``app.llm.prompt`` — ``build_system_prompt``, ``parse_compose_metadata``,
  ``_format_container_ports``.
- ``app.llm.soul`` — ``read_soul`` / ``update_soul`` (persistent ``soul.md``).
- ``app.llm.tools`` — ``TOOLS`` (30 tool definitions) and ``execute_tool``.
- ``app.llm.web`` — WebClaw/Firecrawl helpers (``firecrawl_search``,
  ``firecrawl_scrape``, ``firecrawl_map``).

Internal sub-modules resolve monkeypatch-sensitive symbols (``agent_manager``,
``firecrawl_*``, ``build_system_prompt``, ``execute_tool``, ``LLMClient``)
through this namespace at call time, so ``app.llm.client.<symbole>`` patches
from the tests keep taking effect (pattern a — façade ré-export, identique au
refactor docker_manager).
"""

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from app.config import load_settings
from app.agent_manager.client import agent_manager

from app.llm.constants import (
    HUMAN_VALIDATION_MARKER,
    MAX_TOOL_ROUNDS,
    _DEFAULT_WEB_ENDPOINT,
    _TOOLS_DOCKER_AGENT_PARAM,
)
from app.llm.prompt import (
    build_system_prompt,
    _format_container_ports,
    parse_compose_metadata,
)
from app.llm.soul import (
    _soul_path,
    read_soul,
    update_soul,
)
from app.llm.tools import (
    TOOLS,
    _format_stack_result,
    execute_tool,
)
from app.llm.web import (
    _firecrawl_headers,
    _get_web_endpoint,
    firecrawl_map,
    firecrawl_scrape,
    firecrawl_search,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LLM Client
# ---------------------------------------------------------------------------


class LLMClient:
    """Async client for an OpenAI-compatible chat completions endpoint."""

    def __init__(self) -> None:
        settings = load_settings()
        llm_cfg = settings.get("llm", {}) or {}
        self.endpoint: str = (llm_cfg.get("endpoint") or "").rstrip("/")
        self.api_key: str = llm_cfg.get("api_key") or ""
        self.model: str = llm_cfg.get("model") or ""
    # -- configuration -------------------------------------------------------

    def is_configured(self) -> bool:
        """Return ``True`` when both endpoint and model are set."""
        return bool(self.endpoint and self.model)

    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    # -- non-streaming chat --------------------------------------------------

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
    ) -> Dict[str, Any]:
        """Call the OpenAI-compatible ``/chat/completions`` endpoint.

        Returns the full response JSON dict.
        Raises ``RuntimeError`` if the client is not configured or the API
        returns an error.
        """
        if not self.is_configured():
            raise RuntimeError("LLM client is not configured (endpoint/model missing).")

        url = f"{self.endpoint}/chat/completions"
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice

        try:
            async with httpx.AsyncClient(timeout=120.0) as http:
                resp = await http.post(url, json=body, headers=self._headers())
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error("LLM API HTTP error %s: %s", exc.response.status_code, exc.response.text)
            raise RuntimeError(f"LLM API error {exc.response.status_code}: {exc.response.text}") from exc
        except httpx.RequestError as exc:
            logger.error("LLM API request error: %s", exc)
            raise RuntimeError(f"LLM API request error: {exc}") from exc

    # -- streaming chat ------------------------------------------------------

    async def chat_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ):
        """Stream chat completions via Server-Sent Events.

        Yields ``delta`` content strings (or full chunk dicts for tool calls)
        as they arrive from the API.
        """
        if not self.is_configured():
            raise RuntimeError("LLM client is not configured (endpoint/model missing).")

        url = f"{self.endpoint}/chat/completions"
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        try:
            async with httpx.AsyncClient(timeout=180.0) as http:
                async with http.stream("POST", url, json=body, headers=self._headers()) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        yield chunk
        except httpx.HTTPStatusError as exc:
            logger.error("LLM stream HTTP error %s: %s", exc.response.status_code, exc.response.text)
            raise RuntimeError(f"LLM stream error {exc.response.status_code}: {exc.response.text}") from exc
        except httpx.RequestError as exc:
            logger.error("LLM stream request error: %s", exc)
            raise RuntimeError(f"LLM stream request error: {exc}") from exc


# ---------------------------------------------------------------------------
# Chat loop with tool calls
# ---------------------------------------------------------------------------


async def run_chat(
    user_message: str,
    conversation_history: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Run a full chat interaction with an agentic tool-calling loop.

    Steps:
    1. Build system prompt with live multi-agent Docker context.
    2. Call the LLM with messages + tools.
    3. If the LLM returns ``tool_calls``, execute them.
    4. Append tool results as ``tool`` role messages.
    5. Repeat until the LLM returns a final text response (no tool calls)
       or the round limit is reached.
    6. Return ``{response, tool_calls_made, needs_human_validation}``.

    For ``exec_in_container``: the tool is *not* executed; instead the
    command is added to ``needs_human_validation`` and a placeholder message
    is sent back to the LLM explaining that human validation is required.
    """
    llm = LLMClient()
    if not llm.is_configured():
        return {
            "response": "Le LLM n'est pas configuré. Veuillez définir llm.endpoint et llm.model dans settings.yaml.",
            "tool_calls_made": [],
            "needs_human_validation": [],
        }

    # Build the full message list
    system_prompt = await build_system_prompt()
    messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    messages.extend(conversation_history)
    messages.append({"role": "user", "content": user_message})

    tool_calls_made: List[Dict[str, Any]] = []
    needs_human_validation: List[Dict[str, Any]] = []
    final_response = ""

    logger.info("run_chat start: %d rounds max", MAX_TOOL_ROUNDS)

    for round_idx in range(MAX_TOOL_ROUNDS):
        round_num = round_idx + 1
        logger.info("run_chat round %d/%d", round_num, MAX_TOOL_ROUNDS)
        if round_num >= MAX_TOOL_ROUNDS - 3:
            logger.warning(
                "run_chat approaching round limit (%d/%d), "
                "tool_calls so far: %s",
                round_num,
                MAX_TOOL_ROUNDS,
                [tc["name"] for tc in tool_calls_made],
            )
        try:
            result = await llm.chat(messages, tools=TOOLS, tool_choice="auto")
        except RuntimeError as exc:
            return {
                "response": f"Erreur LLM: {exc}",
                "tool_calls_made": tool_calls_made,
                "needs_human_validation": needs_human_validation,
            }

        choice = (result.get("choices") or [{}])[0]
        message = choice.get("message") or {}

        # Check for tool calls
        tool_calls = message.get("tool_calls")
        assistant_content = message.get("content") or ""

        if tool_calls:
            # Append the assistant message (with tool_calls) to the conversation
            messages.append({
                "role": "assistant",
                "content": assistant_content,
                "tool_calls": tool_calls,
            })

            # Execute each tool call
            for tc in tool_calls:
                fn = tc.get("function") or {}
                tool_name = fn.get("name", "")
                try:
                    tool_args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    tool_args = {}

                tool_call_id = tc.get("id", "")

                # Record the call
                call_record = {
                    "name": tool_name,
                    "arguments": tool_args,
                    "id": tool_call_id,
                }
                tool_calls_made.append(call_record)
                logger.info(
                    "run_chat tool call: %s args=%s",
                    tool_name,
                    {k: v for k, v in tool_args.items() if k != "compose_content"},
                )

                # Execute
                tool_result = await execute_tool(tool_name, tool_args)
                logger.info(
                    "run_chat tool result (%s): %s",
                    tool_name,
                    tool_result[:200],
                )

                # Handle human-validation tools
                if tool_result.startswith(HUMAN_VALIDATION_MARKER):
                    needs_human_validation.append({
                        "name": tool_name,
                        "arguments": tool_args,
                        "id": tool_call_id,
                    })
                    # Tell the LLM that this command needs human validation
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
                            f"Agent: {tool_args.get('agent_name', '')}, "
                            f"container: {tool_args.get('container_id', '')}, "
                            f"commande proposée: {tool_args.get('command', '')}. "
                            f"Informe l'utilisateur que la commande est en attente de validation."
                        )
                else:
                    tool_result_msg = tool_result

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": tool_result_msg,
                })

            # Continue the loop for the next LLM call
            continue

        # No tool calls — this is the final response
        final_response = assistant_content
        if not final_response:
            # Some APIs return content in a different field
            final_response = choice.get("text") or message.get("text") or ""
        break

    else:
        # Round limit reached without a final textual response.
        # Provide a clear summary of what was accomplished so far instead of
        # returning an empty response.
        logger.warning(
            "run_chat reached round limit (%d) with %d tool calls",
            MAX_TOOL_ROUNDS,
            len(tool_calls_made),
        )
        if not final_response:
            tool_summary = "\n".join(
                [f"- {tc['name']}" for tc in tool_calls_made]
            ) or "(aucun outil appelé)"
            final_response = (
                "J'ai atteint la limite d'interactions pour cette requête. "
                "Voici ce que j'ai fait jusqu'à présent:\n" + tool_summary
            )

    # Build the full conversation history (excluding the system prompt)
    # so the frontend can persist it and send it back on the next message.
    full_history = [m for m in messages if m["role"] != "system"]

    return {
        "response": final_response,
        "tool_calls_made": tool_calls_made,
        "needs_human_validation": needs_human_validation,
        "history": full_history,
    }