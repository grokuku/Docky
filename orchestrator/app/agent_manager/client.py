"""Agent manager for the Docky orchestrator (façade).

This module is the **façade** of the ``app.agent_manager`` sub-package. It
keeps the ``AgentManager`` HTTP/streaming layer and re-exports the cohesive
sub-modules so existing imports (routes, LLM, tests) keep working unchanged:

- ``app.agent_manager.paths`` — ``translate_path`` (path mappings).
- ``app.agent_manager.cache`` — cache persistence + per-agent and aggregate
  stale-while-revalidate caches (``_load_cache``, ``_save_cache``,
  ``refresh_cache``, ``invalidate_cache``, ``_rebuild_aggregate_cache``,
  ``get_cached_*``, ``refresh_all_caches``...).
- ``app.agent_manager.events`` — event-driven refresh (``start_background_refresh``,
  ``_connect_agent_events``, ``_handle_agent_event``, ``_incremental_refresh``).

The extracted methods are assigned back onto ``AgentManager`` in this façade.
The singleton ``agent_manager = AgentManager()`` stays here so the instance is
unique across ``app.routes.api``, ``app.llm.client`` and ``app.main``.
"""

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from app.config import get_data_dir, load_settings
from app.agent_manager import cache as _cache
from app.agent_manager import events as _events
from app.agent_manager import paths as _paths

logger = logging.getLogger(__name__)

# Per-agent cache entries older than this many seconds are refreshed from the
# network when rebuilding the aggregate cache (kept re-exported here for
# backward-compatible namespace).
_AGENT_CACHE_TTL = _cache._AGENT_CACHE_TTL

# Timeout profile for streamed (SSE) agent requests.
#
# The read timeout is PER READ: as long as the agent keeps sending output
# lines, no timeout fires. It only triggers after a silence longer than the
# read timeout. It must stay ABOVE the agent-side idle timeout (120 s): the
# agent kills the command first and reports a clean SSE ``done``/``error``
# event before httpx would time out and abort the connection.
STREAM_TIMEOUT = httpx.Timeout(connect=10, read=150, write=30, pool=10)


class AgentManager:
    """Manage communication with one or more remote Docky agents."""

    def __init__(self):
        self.agents: Dict[str, Dict[str, Any]] = {}  # name -> {url, api_key, status, last_check}
        self.cache: Dict[str, Dict[str, Any]] = {}   # name -> {containers, stacks, ports, timestamp}
        # Aggregate stale-while-revalidate cache (for "all" views)
        self._cache = {
            "containers": {"data": None, "timestamp": 0, "pending": False},
            "stacks": {"data": None, "timestamp": 0, "pending": False},
            "ports": {"data": None, "timestamp": 0, "pending": False},
        }
        self._cache_path = str(get_data_dir() / "cache.json")
        self._load_cache()
        self._bg_task = None
        self._ws_tasks: Dict[str, asyncio.Task] = {}
        self._event_debounce_timers: Dict[str, asyncio.Task] = {}
        # Broadcast callback injected by app.routes.api (breaks the latent
        # app.agent_manager.client <-> app.routes.api import cycle).
        self.broadcast_agent_event = None
        self._load_agents()

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _load_agents(self):
        """Load agents from ``settings.yaml``.

        Each agent may carry optional TLS policy keys:

        - ``tls_verify`` (bool, default ``True``): whether to verify the
          agent's TLS certificate on HTTPS/WSS connections. NEVER defaults to
          ``False``. If explicitly set to ``False`` a clear ``WARNING`` is
          logged.
        - ``ca_cert`` (str path, optional): a custom CA bundle / certificate
          to trust instead of the system store.
        """
        settings = load_settings()
        agents = settings.get("agents", [])
        for agent in agents:
            tls_verify = agent.get("tls_verify", True)
            if isinstance(tls_verify, str):
                tls_verify = tls_verify.strip().lower() in ("1", "true", "yes", "on")
            tls_verify = bool(tls_verify)
            ca_cert = agent.get("ca_cert") or None
            if not tls_verify:
                logger.warning(
                    "Agent '%s': TLS certificate verification is DISABLED "
                    "(tls_verify=false). The connection can be intercepted "
                    "(MITM). Prefer HTTPS with a trusted certificate, a "
                    "private network, or a VPN instead.",
                    agent["name"],
                )
            self.agents[agent["name"]] = {
                "url": agent["url"],
                "api_key": agent["api_key"],
                "status": "unknown",
                "last_check": 0,
                "tls_verify": tls_verify,
                "ca_cert": ca_cert,
            }

    def reload(self):
        """Reload the agent configuration from disk."""
        self.agents = {}
        self.cache = {}
        self._cache = {
            "containers": {"data": None, "timestamp": 0, "pending": False},
            "stacks": {"data": None, "timestamp": 0, "pending": False},
            "ports": {"data": None, "timestamp": 0, "pending": False},
        }
        self._load_agents()

    def list_agents(self) -> List[Dict[str, Any]]:
        """Return the list of agents with their current status."""
        return [
            {"name": name, "url": info["url"], "status": info["status"]}
            for name, info in self.agents.items()
        ]

    # ------------------------------------------------------------------
    # Cache persistence (extracted to app.agent_manager.cache)
    # ------------------------------------------------------------------

    _load_cache = _cache._load_cache
    _save_cache = _cache._save_cache

    # ------------------------------------------------------------------
    # Path mappings
    # ------------------------------------------------------------------

    translate_path = _paths.translate_path

    # ------------------------------------------------------------------
    # Health checks
    # ------------------------------------------------------------------

    async def ping_agent(self, name: str) -> bool:
        """Ping an agent to verify it is reachable."""
        if name not in self.agents:
            return False
        agent = self.agents[name]
        try:
            async with httpx.AsyncClient(timeout=5, **self._agent_tls_options(agent)) as client:
                resp = await client.get(f"{agent['url']}/agent/health")
                if resp.status_code == 200:
                    agent["status"] = "online"
                    agent["last_check"] = time.time()
                    return True
        except Exception as exc:
            logger.warning("ping_agent failed for '%s': %s", name, exc)
        agent["status"] = "offline"
        agent["last_check"] = time.time()
        return False

    async def ping_all(self):
        """Ping every configured agent in parallel."""
        tasks = [self.ping_agent(name) for name in self.agents]
        if tasks:
            await asyncio.gather(*tasks)

    # ------------------------------------------------------------------
    # TLS helpers
    # ------------------------------------------------------------------

    def _agent_tls_options(self, agent: Dict[str, Any]) -> Dict[str, Any]:
        """Return the ``httpx`` ``verify`` kwargs for an agent's TLS policy.

        ``verify`` is NEVER ``False`` unless the agent explicitly opted out via
        ``tls_verify: false`` (warned at load time). If a ``ca_cert`` is
        configured it is used as the CA bundle path.
        """
        if agent.get("ca_cert"):
            return {"verify": agent["ca_cert"]}
        return {"verify": bool(agent.get("tls_verify", True))}

    def _agent_ws_ssl(self, agent: Dict[str, Any]):
        """Return an ``ssl.SSLContext`` for WebSocket/WSS connections.

        Returns ``None`` when no custom CA / verification override is needed,
        in which case ``websockets`` performs its default (secure) TLS
        verification.
        """
        import ssl

        ca_cert = agent.get("ca_cert")
        tls_verify = bool(agent.get("tls_verify", True))
        if not ca_cert and tls_verify:
            return None
        ctx = ssl.create_default_context(cafile=ca_cert) if ca_cert else ssl.create_default_context()
        if not tls_verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _agent_ws_connect_kwargs(self, agent: Dict[str, Any]) -> Dict[str, Any]:
        """Build ``websockets.connect(...)`` kwargs for an agent connection.

        Centralises:
        - the API key as ``Authorization: Bearer <key>`` (NEVER in the URL /
          query string, so it cannot leak into proxy or access logs), and
        - the TLS policy (custom CA / explicit verification opt-out).
        """
        kwargs: Dict[str, Any] = {}
        api_key = agent.get("api_key", "")
        if api_key:
            kwargs["additional_headers"] = {"Authorization": f"Bearer {api_key}"}
        ssl = self._agent_ws_ssl(agent)
        if ssl is not None:
            kwargs["ssl"] = ssl
        return kwargs

    # ------------------------------------------------------------------
    # Low-level request helper
    # ------------------------------------------------------------------

    async def _request(self, agent_name: str, method: str, path: str, timeout: float = 30, **kwargs) -> Any:
        """Perform an HTTP request toward a specific agent.

        Automatically injects the ``Authorization: Bearer <key>`` header.
        *timeout* defaults to 30 seconds but should be raised (e.g. 300) for
        long-running operations such as stack deployments that may pull
        container images.
        """
        if agent_name not in self.agents:
            raise ValueError(f"Agent '{agent_name}' not found")
        agent = self.agents[agent_name]
        url = f"{agent['url']}{path}"
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {agent['api_key']}"
        async with httpx.AsyncClient(timeout=timeout, **self._agent_tls_options(agent)) as client:
            resp = await client.request(method, url, headers=headers, **kwargs)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if content_type.startswith("application/json"):
                return resp.json()
            return resp.text

    def _parse_sse_event(self, event: Optional[str], data_lines: List[str]) -> Optional[Dict[str, Any]]:
        """Convert one SSE block into a normalized event dict.

        Returns ``None`` for comments / keep-alive lines. Recognised events:

        - ``output`` → ``{"type": "output", "line": str}``
        - ``done`` → ``{"type": "done", "success": bool, "output": str, "error": str}``
        - ``error`` → ``{"type": "error", "error": str}``
        """
        if not event or not data_lines:
            return None
        raw = "\n".join(data_lines)
        try:
            data = json.loads(raw)
        except Exception:
            data = {"raw": raw}
        if event == "output":
            return {"type": "output", "line": data.get("line", "")}
        if event == "done":
            return {
                "type": "done",
                "success": bool(data.get("success", True)),
                "output": data.get("output", ""),
                # The agent embeds the real failure message in the final
                # ``done`` event's ``error`` field; propagate it so
                # :meth:`_consume_stream` can surface it to JSON/LLM callers.
                "error": data.get("error", ""),
            }
        if event == "error":
            return {"type": "error", "error": data.get("error", "Erreur inconnue")}
        return None

    async def _stream_request(self, agent_name: str, method: str, path: str, **kwargs) -> AsyncIterator[Dict[str, Any]]:
        """Open a streaming HTTP request to an agent and yield parsed SSE events.

        Injects the ``Authorization: Bearer <key>`` header like :meth:`_request`
        and uses :data:`STREAM_TIMEOUT` (read timeout per-read) so long-running
        operations keep streaming for as long as the agent emits output.

        Yields dicts of the form produced by :meth:`_parse_sse_event`.
        """
        if agent_name not in self.agents:
            raise ValueError(f"Agent '{agent_name}' not found")
        agent = self.agents[agent_name]
        url = f"{agent['url']}{path}"
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {agent['api_key']}"
        headers["Accept"] = "text/event-stream"
        timeout = kwargs.pop("timeout", STREAM_TIMEOUT)
        async with httpx.AsyncClient(timeout=timeout, **self._agent_tls_options(agent)) as client:
            async with client.stream(method, url, headers=headers, **kwargs) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", errors="replace")
                    raise RuntimeError(f"Agent returned HTTP {resp.status_code}: {body[:500]}")
                content_type = resp.headers.get("content-type", "")
                if content_type and "text/event-stream" not in content_type:
                    # A non-streaming answer is unexpected here (agent not
                    # upgraded, or a JSON error): surface it instead of
                    # silently yielding nothing.
                    body = (await resp.aread()).decode("utf-8", errors="replace")
                    raise RuntimeError(
                        f"Agent returned non-streaming response ({content_type}): {body[:500]}"
                    )
                event: Optional[str] = None
                data_lines: List[str] = []
                async for line in resp.aiter_lines():
                    if not line:
                        evt = self._parse_sse_event(event, data_lines)
                        if evt is not None:
                            yield evt
                        event = None
                        data_lines = []
                        continue
                    if line.startswith("event:"):
                        event = line[len("event:"):].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[len("data:"):].strip())
                # Flush a trailing event if the stream closed without a blank line
                evt = self._parse_sse_event(event, data_lines)
                if evt is not None:
                    yield evt

    async def _consume_stream(self, agent_name: str, method: str, path: str) -> Dict[str, Any]:
        """Consume a streamed action and return an aggregated JSON result.

        Used by the JSON helpers (kept for LLM tools and other non-browser
        callers): the SSE output lines are accumulated and the final ``done``
        event decides success.
        """
        lines: List[str] = []
        success = True
        error = ""
        try:
            async for evt in self._stream_request(agent_name, method, path):
                if evt["type"] == "output":
                    if evt.get("line"):
                        lines.append(evt["line"])
                elif evt["type"] == "done":
                    success = bool(evt.get("success", True))
                    # The agent embeds the real failure message in the final
                    # ``done`` event's ``error`` field; surface it so JSON/LLM
                    # callers get a consistent ``{success, output, error}`` dict.
                    if evt.get("error"):
                        error = evt["error"]
                elif evt["type"] == "error":
                    success = False
                    error = evt.get("error", "Erreur inconnue")
        except Exception as e:
            return {"success": False, "error": str(e)}
        result: Dict[str, Any] = {"success": success, "output": "\n".join(lines)}
        if error:
            result["error"] = error
        return result

    def _agent_error(self, exc: Exception) -> Dict[str, Any]:
        """Turn an exception raised while talking to an agent into an error dict.

        Transport-level failures (agent unreachable / HTTP error) are tagged
        with ``unreachable: True`` so :func:`_check_agent_error` can tell them
        apart from *business* errors returned by the agent itself.
        """
        return {"success": False, "error": str(exc), "unreachable": True}

    # ------------------------------------------------------------------
    # Containers
    # ------------------------------------------------------------------

    async def get_containers(self, agent_name: str) -> List[Dict[str, Any]]:
        """List all containers on an agent."""
        try:
            return await self._request(agent_name, "GET", "/agent/containers")
        except Exception as exc:
            logger.error("get_containers failed for agent '%s': %s", agent_name, exc)
            return []

    async def get_container(self, agent_name: str, container_id: str) -> Optional[Dict[str, Any]]:
        """Return details for a single container."""
        try:
            return await self._request(
                agent_name, "GET", f"/agent/containers/{container_id}"
            )
        except Exception as exc:
            logger.warning("get_container failed for agent '%s', container '%s': %s", agent_name, container_id, exc)
            return None

    async def get_container_stats(self, agent_name: str, container_id: str) -> Dict[str, Any]:
        """Return CPU/RAM stats for a container."""
        try:
            return await self._request(
                agent_name, "GET", f"/agent/containers/{container_id}/stats"
            )
        except Exception as exc:
            logger.warning("get_container_stats failed for agent '%s', container '%s': %s", agent_name, container_id, exc)
            return {}

    async def get_container_logs(self, agent_name: str, container_id: str, tail: int = 100) -> List[Dict]:
        """Return the last *tail* log lines with timestamps and stream info."""
        try:
            data = await self._request(
                agent_name, "GET", f"/agent/containers/{container_id}/logs",
                params={"tail": tail},
            )
            if isinstance(data, dict):
                return data.get("lines", [])
            return []
        except Exception as exc:
            logger.warning("get_container_logs failed for agent '%s', container '%s': %s", agent_name, container_id, exc)
            return []

    async def exec_container(self, agent_name: str, container_id: str, command: str) -> Dict[str, Any]:
        """Execute a one-shot command in a container on an agent."""
        try:
            return await self._request(
                agent_name, "POST",
                f"/agent/containers/{container_id}/exec",
                json={"command": command},
            )
        except Exception as e:
            logger.error("exec_container failed for agent '%s', container '%s': %s", agent_name, container_id, e)
            return {"success": False, "error": str(e)}

    async def start_container(self, agent_name: str, container_id: str) -> bool:
        """Start a container on an agent."""
        try:
            data = await self._request(
                agent_name, "POST", f"/agent/containers/{container_id}/start"
            )
            if isinstance(data, dict):
                return data.get("success", False)
            return True
        except Exception:
            return False

    async def stop_container(self, agent_name: str, container_id: str) -> bool:
        """Stop a container on an agent."""
        try:
            data = await self._request(
                agent_name, "POST", f"/agent/containers/{container_id}/stop"
            )
            if isinstance(data, dict):
                return data.get("success", False)
            return True
        except Exception as exc:
            logger.error("stop_container failed for agent '%s', container '%s': %s", agent_name, container_id, exc)
            return False

    async def restart_container(self, agent_name: str, container_id: str) -> bool:
        """Restart a container on an agent."""
        try:
            data = await self._request(
                agent_name, "POST", f"/agent/containers/{container_id}/restart"
            )
            if isinstance(data, dict):
                return data.get("success", False)
            return True
        except Exception as exc:
            logger.error("restart_container failed for agent '%s', container '%s': %s", agent_name, container_id, exc)
            return False

    async def check_update(self, agent_name: str, container_id: str) -> Dict[str, Any]:
        """Check if a container image has an update available on the registry."""
        try:
            return await self._request(
                agent_name, "GET", f"/agent/containers/{container_id}/update-check"
            )
        except Exception as exc:
            logger.warning("check_update failed for agent '%s', container '%s': %s", agent_name, container_id, exc)
            return {"update_available": False, "error": "Agent unreachable"}

    async def get_container_edit_spec(self, agent_name: str, container_id: str) -> Optional[Dict]:
        """Return the full spec of a container for editing."""
        try:
            return await self._request(
                agent_name, "GET", f"/agent/containers/{container_id}/edit-spec"
            )
        except Exception as exc:
            logger.warning("get_container_edit_spec failed for agent '%s', container '%s': %s", agent_name, container_id, exc)
            return None

    async def update_container(self, agent_name: str, container_id: str, spec: Dict) -> Dict:
        """Apply changes to a container on an agent."""
        try:
            result = await self._request(
                agent_name, "POST", f"/agent/containers/{container_id}/update",
                json=spec,
            )
            if isinstance(result, dict) and result.get("success"):
                await self.invalidate_cache(agent_name)
            return result
        except Exception as e:
            return self._agent_error(e)

    async def stream_update_container_image(self, agent_name: str, container_id: str) -> AsyncIterator[Dict[str, Any]]:
        """Stream an image update (pull + recreate) for a container on an agent.

        Yields ``output`` / ``done`` / ``error`` events (see :meth:`_parse_sse_event`).
        """
        async for evt in self._stream_request(agent_name, "POST", f"/agent/containers/{container_id}/update-image"):
            yield evt

    async def update_container_image(self, agent_name: str, container_id: str) -> Dict[str, Any]:
        """Pull the latest image and recreate a container on an agent (JSON result).

        Consumes the streamed action and aggregates the output. Invalidates the
        agent cache after a successful update so the UI shows the new state.
        """
        result = await self._consume_stream(agent_name, "POST", f"/agent/containers/{container_id}/update-image")
        if result.get("success"):
            await self.invalidate_cache(agent_name)
        return result

    # ------------------------------------------------------------------
    # Stacks
    # ------------------------------------------------------------------

    async def get_stacks(self, agent_name: str) -> List[Dict[str, Any]]:
        """List all stacks on an agent."""
        try:
            return await self._request(agent_name, "GET", "/agent/stacks")
        except Exception as exc:
            logger.error("get_stacks failed for agent '%s': %s", agent_name, exc)
            return []

    async def get_stack_files(self, agent_name: str, stack_name: str, include_hidden: bool = False) -> List[Dict[str, Any]]:
        """List files in a stack directory on an agent.

        By default only the *editable* files (Compose + ``.env``) are listed.
        Pass ``include_hidden=True`` to get the complete directory listing
        (frontend toggle « afficher tous les fichiers » / LLM tool).
        """
        try:
            data = await self._request(
                agent_name, "GET", f"/agent/stacks/{stack_name}/files",
                params={"include_hidden": str(include_hidden).lower()},
            )
            if isinstance(data, dict):
                return data.get("files", [])
            return []
        except Exception as exc:
            logger.warning("get_stack_files failed for agent '%s', stack '%s': %s", agent_name, stack_name, exc)
            return []

    async def get_stack_file(self, agent_name: str, stack_name: str, filename: str) -> Optional[str]:
        """Read a file from a stack directory on an agent."""
        try:
            return await self._request(
                agent_name, "GET", f"/agent/stacks/{stack_name}/files/{filename}"
            )
        except Exception as exc:
            logger.warning("get_stack_file failed for agent '%s', stack '%s', file '%s': %s", agent_name, stack_name, filename, exc)
            return None

    async def get_stack_files_with_content(self, agent_name: str, stack_name: str, include_hidden: bool = False) -> dict:
        """List all files in a stack WITH their content in a single call.

        By default only the *editable* files (Compose + ``.env``) are returned.
        Pass ``include_hidden=True`` for the complete listing.

        Returns a dict with a ``files`` key containing a list of
        ``{"filename": str, "content": str | None, "size": int}`` objects.
        """
        try:
            data = await self._request(
                agent_name, "GET", f"/agent/stacks/{stack_name}/files-with-content",
                params={"include_hidden": str(include_hidden).lower()},
                timeout=30,
            )
            if isinstance(data, dict):
                return data
            return {"files": []}
        except Exception as exc:
            logger.warning("get_stack_files_with_content failed for agent '%s', stack '%s': %s", agent_name, stack_name, exc)
            return {"files": []}

    async def save_stack_file(self, agent_name: str, stack_name: str, filename: str, content: str) -> Dict[str, Any]:
        """Write content to a file in a stack directory on an agent.

        Returns ``{"success": True}`` on success. On failure the dict carries
        ``success: False`` and an ``error`` message. Transport-level failures
        (agent unreachable, HTTP status) are tagged ``unreachable: True`` so
        :func:`app.routes.api._check_agent_error` can tell them apart from a
        *business* error returned by the agent (e.g. stack not found, invalid
        filename), which keeps the real agent message visible in the UI.
        """
        try:
            data = await self._request(
                agent_name, "PUT",
                f"/agent/stacks/{stack_name}/files/{filename}",
                content=content,
                headers={"Content-Type": "text/plain"},
            )
            if isinstance(data, dict) and not data.get("success", True):
                return {"success": False, "error": data.get("error", "Unknown agent error")}
            return {"success": True}
        except httpx.HTTPStatusError as e:
            # The agent answered with an HTTP error: surface its real message.
            message = str(e)
            try:
                payload = e.response.json()
                if isinstance(payload, dict) and payload.get("error"):
                    message = str(payload["error"])
            except Exception:
                pass
            return {"success": False, "error": message}
        except Exception as e:
            return self._agent_error(e)

    async def create_stack(
        self,
        agent_name: str,
        name: str,
        compose: str,
        env: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new stack on an agent."""
        body: Dict[str, Any] = {"name": name, "compose": compose}
        if env is not None:
            body["env"] = env
        try:
            result = await self._request(agent_name, "POST", "/agent/stacks", json=body)
            if isinstance(result, dict) and result.get("success"):
                await self.invalidate_cache(agent_name)
            return result
        except Exception as e:
            return self._agent_error(e)

    async def delete_stack(self, agent_name: str, stack_name: str) -> Dict[str, Any]:
        """Delete a stack on an agent."""
        try:
            result = await self._request(
                agent_name, "DELETE", f"/agent/stacks/{stack_name}"
            )
            if isinstance(result, dict) and result.get("success"):
                await self.invalidate_cache(agent_name)
            return result
        except Exception as e:
            return self._agent_error(e)

    async def stream_deploy_stack(self, agent_name: str, stack_name: str) -> AsyncIterator[Dict[str, Any]]:
        """Stream a stack deploy (down + up) on an agent."""
        async for evt in self._stream_request(agent_name, "POST", f"/agent/stacks/{stack_name}/deploy"):
            yield evt

    async def deploy_stack(self, agent_name: str, stack_name: str) -> Dict[str, Any]:
        """Deploy (down + up) a stack on an agent (JSON result)."""
        result = await self._consume_stream(agent_name, "POST", f"/agent/stacks/{stack_name}/deploy")
        if result.get("success"):
            await self.invalidate_cache(agent_name)
        return result

    async def stream_start_stack(self, agent_name: str, stack_name: str) -> AsyncIterator[Dict[str, Any]]:
        """Stream a stack start (``docker compose up -d``) on an agent.

        The agent runs ``up -d`` so the action starts existing containers AND
        creates the missing ones (intended semantics of the "Démarrer" button).
        """
        async for evt in self._stream_request(agent_name, "POST", f"/agent/stacks/{stack_name}/start"):
            yield evt

    async def start_stack(self, agent_name: str, stack_name: str) -> Dict[str, Any]:
        """Start a stack (``docker compose up -d``) on an agent (JSON result)."""
        result = await self._consume_stream(agent_name, "POST", f"/agent/stacks/{stack_name}/start")
        if result.get("success"):
            await self.invalidate_cache(agent_name)
        return result

    async def stream_stop_stack(self, agent_name: str, stack_name: str) -> AsyncIterator[Dict[str, Any]]:
        """Stream a stack stop on an agent."""
        async for evt in self._stream_request(agent_name, "POST", f"/agent/stacks/{stack_name}/stop"):
            yield evt

    async def stop_stack(self, agent_name: str, stack_name: str) -> Dict[str, Any]:
        """Stop a stack on an agent (JSON result)."""
        result = await self._consume_stream(agent_name, "POST", f"/agent/stacks/{stack_name}/stop")
        if result.get("success"):
            await self.invalidate_cache(agent_name)
        return result

    async def stream_restart_stack(self, agent_name: str, stack_name: str) -> AsyncIterator[Dict[str, Any]]:
        """Stream a stack restart on an agent."""
        async for evt in self._stream_request(agent_name, "POST", f"/agent/stacks/{stack_name}/restart"):
            yield evt

    async def restart_stack(self, agent_name: str, stack_name: str) -> Dict[str, Any]:
        """Restart a stack on an agent (JSON result)."""
        result = await self._consume_stream(agent_name, "POST", f"/agent/stacks/{stack_name}/restart")
        if result.get("success"):
            await self.invalidate_cache(agent_name)
        return result

    async def stream_update_stack(self, agent_name: str, stack_name: str) -> AsyncIterator[Dict[str, Any]]:
        """Stream a stack update (pull + up -d) on an agent."""
        async for evt in self._stream_request(agent_name, "POST", f"/agent/stacks/{stack_name}/update"):
            yield evt

    async def update_stack(self, agent_name: str, stack_name: str) -> Dict[str, Any]:
        """Update a stack (pull + up -d) on an agent (JSON result)."""
        result = await self._consume_stream(agent_name, "POST", f"/agent/stacks/{stack_name}/update")
        if result.get("success"):
            await self.invalidate_cache(agent_name)
        return result

    async def check_stack_update(self, agent_name: str, stack_name: str) -> Dict[str, Any]:
        """Check if a stack has an image update available (no pull)."""
        try:
            return await self._request(
                agent_name, "GET", f"/agent/stacks/{stack_name}/update-check", timeout=30
            )
        except Exception as e:
            logger.warning("check_stack_update failed for agent '%s', stack '%s': %s", agent_name, stack_name, e)
            return {"update_available": False, "error": str(e)}

    async def get_stack_logs(self, agent_name: str, stack_name: str, tail: int = 100) -> Dict[str, Any]:
        """Return the last ``tail`` log lines for a stack on an agent.

        The agent runs ``docker compose logs --tail=N`` (non-streamed) with a
        fallback to per-container logs. Returns ``{"success": bool,
        "output": str}``; transport errors are tagged ``unreachable``.
        """
        try:
            data = await self._request(
                agent_name, "GET", f"/agent/stacks/{stack_name}/logs",
                params={"tail": tail}, timeout=30,
            )
            if isinstance(data, dict):
                return data
            return {"success": False, "error": "Unexpected agent response"}
        except Exception as e:
            return self._agent_error(e)

    async def import_stack(
        self,
        agent_name: str,
        source_path: str,
        stack_name: Optional[str] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Import a stack from an external folder (e.g. Dockge) on an agent.

        The agent copies the ``docker-compose.yml`` and ``.env`` from the
        source folder and converts relative paths to absolute paths.

        When *dry_run* is True, the agent does not copy any file: it only
        returns a preview of the converted compose file along with the list
        of path conversions and warnings.
        """
        body: Dict[str, Any] = {"source_path": source_path}
        if stack_name:
            body["stack_name"] = stack_name
        if dry_run:
            body["dry_run"] = True
        try:
            result = await self._request(
                agent_name, "POST", "/agent/stacks/import", json=body, timeout=60
            )
            if isinstance(result, dict) and result.get("success"):
                await self.invalidate_cache(agent_name)
            return result
        except Exception as e:
            return self._agent_error(e)

    async def set_permissions(
        self,
        agent_name: str,
        stack_name: str,
        filename: str,
        mode: str,
    ) -> Dict[str, Any]:
        """Change file permissions (chmod) on a stack file."""
        try:
            return await self._request(
                agent_name, "PUT",
                f"/agent/stacks/{stack_name}/files/{filename}/permissions",
                json={"mode": mode},
            )
        except Exception as e:
            return self._agent_error(e)

    # ------------------------------------------------------------------
    # Git history
    # ------------------------------------------------------------------

    async def get_stack_history(self, agent_name: str, stack_name: str) -> list:
        """Return the git history (commits) for a stack."""
        try:
            return await self._request(agent_name, "GET", f"/agent/stacks/{stack_name}/history")
        except Exception as exc:
            logger.warning("get_stack_history failed for agent '%s', stack '%s': %s", agent_name, stack_name, exc)
            return []

    async def get_stack_version(self, agent_name: str, stack_name: str, hash: str) -> dict:
        """Return the content of a specific git version for a stack."""
        try:
            return await self._request(agent_name, "GET", f"/agent/stacks/{stack_name}/history/{hash}")
        except Exception as exc:
            logger.warning("get_stack_version failed for agent '%s', stack '%s': %s", agent_name, stack_name, exc)
            return None

    async def restore_stack_version(self, agent_name: str, stack_name: str, hash: str) -> dict:
        """Restore (checkout) a specific git version for a stack."""
        try:
            return await self._request(agent_name, "POST", f"/agent/stacks/{stack_name}/history/restore/{hash}")
        except Exception as e:
            logger.error("restore_stack_version failed for agent '%s', stack '%s': %s", agent_name, stack_name, e)
            return {"success": False, "error": str(e)}

    async def update_git_history_settings(self, agent_name: str, settings: Dict[str, Any]) -> Dict[str, Any]:
        """Propagate git/history retention settings to an agent."""
        try:
            return await self._request(
                agent_name, "PUT", "/agent/settings/git-history", json=settings
            )
        except Exception as e:
            logger.error("update_git_history_settings failed for agent '%s': %s", agent_name, e)
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # Ports
    # ------------------------------------------------------------------

    async def get_ports(self, agent_name: str) -> List[Dict[str, Any]]:
        """Return all ports in use on an agent host."""
        try:
            return await self._request(agent_name, "GET", "/agent/ports")
        except Exception as exc:
            logger.error("get_ports failed for agent '%s': %s", agent_name, exc)
            return []

    async def clean_agent(self, agent_name: str) -> Dict[str, Any]:
        """Clean unused Docker resources on an agent (docker system prune)."""
        try:
            return await self._request(
                agent_name, "POST", "/agent/system/prune", timeout=120
            )
        except Exception as e:
            logger.error("clean_agent failed for agent '%s': %s", agent_name, e)
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # Per-agent + aggregate cache (extracted to app.agent_manager.cache)
    # ------------------------------------------------------------------

    refresh_cache = _cache.refresh_cache
    invalidate_cache = _cache.invalidate_cache
    _get_cached_containers = _cache._get_cached_containers
    _get_cached_stacks = _cache._get_cached_stacks
    _get_cached_ports = _cache._get_cached_ports
    _get_cached_or_refresh = _cache._get_cached_or_refresh
    _refresh_cache_entry = _cache._refresh_cache_entry

    # ------------------------------------------------------------------
    # Event-driven refresh (extracted to app.agent_manager.events)
    # ------------------------------------------------------------------

    start_background_refresh = _events.start_background_refresh
    _connect_agent_events = _events._connect_agent_events
    _handle_agent_event = _events._handle_agent_event
    _incremental_refresh = _events._incremental_refresh

    _rebuild_aggregate_cache = _cache._rebuild_aggregate_cache
    ensure_cache = _cache.ensure_cache
    get_cached_containers = _cache.get_cached_containers
    get_cached_stacks = _cache.get_cached_stacks
    get_cached_ports = _cache.get_cached_ports
    refresh_all_caches = _cache.refresh_all_caches

    # ------------------------------------------------------------------
    # Global views (aggregate across all agents)
    # ------------------------------------------------------------------

    async def get_all_containers(self) -> List[Dict[str, Any]]:
        """Aggregate containers from all agents, tagging each with ``agent_name``."""
        all_containers: List[Dict[str, Any]] = []
        tasks = [self.get_containers(name) for name in self.agents]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for name, result in zip(self.agents.keys(), results):
            if isinstance(result, list):
                for container in result:
                    if isinstance(container, dict):
                        container["agent_name"] = name
                    all_containers.append(container)
        return all_containers

    async def get_all_stacks(self) -> List[Dict[str, Any]]:
        """Aggregate stacks from all agents, tagging each with ``agent_name``."""
        all_stacks: List[Dict[str, Any]] = []
        tasks = [self.get_stacks(name) for name in self.agents]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for name, result in zip(self.agents.keys(), results):
            if isinstance(result, list):
                for stack in result:
                    if isinstance(stack, dict):
                        stack["agent_name"] = name
                    all_stacks.append(stack)
        return all_stacks

    async def get_all_ports(self) -> List[Dict[str, Any]]:
        """Aggregate ports from all agents, tagging each with ``agent_name``."""
        all_ports: List[Dict[str, Any]] = []
        tasks = [self.get_ports(name) for name in self.agents]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for name, result in zip(self.agents.keys(), results):
            if isinstance(result, list):
                for port in result:
                    if isinstance(port, dict):
                        port["agent_name"] = name
                    all_ports.append(port)
        return all_ports


# Instance globale
agent_manager = AgentManager()