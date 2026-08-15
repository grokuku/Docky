"""Agent manager for the Docky orchestrator.

Communicates with remote Docky Agent services over HTTP, replacing the
direct Docker SDK access that was previously provided by
``app.docker_manager.client``.

Each agent is declared in ``settings.yaml`` under the ``agents`` key:

.. code-block:: yaml

    agents:
      - name: "Serveur Principal"
        url: "http://192.168.1.10:8080"
        api_key: "agent-api-key-1"

All network calls are performed asynchronously with ``httpx``.
"""

import asyncio
import json
import logging
import os
import time
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from app.config import get_data_dir, load_settings

logger = logging.getLogger(__name__)

# Per-agent cache entries older than this many seconds are refreshed from the
# network when rebuilding the aggregate cache.
_AGENT_CACHE_TTL = 60.0

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
        self._load_agents()

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _load_agents(self):
        """Load agents from ``settings.yaml``."""
        settings = load_settings()
        agents = settings.get("agents", [])
        for agent in agents:
            self.agents[agent["name"]] = {
                "url": agent["url"],
                "api_key": agent["api_key"],
                "status": "unknown",
                "last_check": 0,
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
    # Cache persistence
    # ------------------------------------------------------------------

    def _load_cache(self):
        """Load cache from disk if available."""
        try:
            if os.path.exists(self._cache_path):
                with open(self._cache_path) as f:
                    saved = json.load(f)
                    if isinstance(saved, dict):
                        self._cache = saved
        except Exception:
            self._cache = {
                "containers": {"data": None, "timestamp": 0, "pending": False},
                "stacks": {"data": None, "timestamp": 0, "pending": False},
                "ports": {"data": None, "timestamp": 0, "pending": False},
            }

    def _save_cache(self):
        """Persist cache to disk."""
        try:
            os.makedirs(os.path.dirname(self._cache_path), exist_ok=True)
            with open(self._cache_path, "w") as f:
                json.dump(self._cache, f, default=str)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Path mappings
    # ------------------------------------------------------------------

    def translate_path(self, agent_name: str, host_path: str) -> str:
        """Translate a host path to the agent's local path using path mappings.

        Each agent can declare a list of ``path_mappings`` in ``settings.yaml``.
        The longest matching host prefix is replaced by the corresponding
        local path. If no mapping matches, the original path is returned.
        """
        settings = load_settings()
        agents = settings.get("agents", []) or []
        for agent in agents:
            if agent.get("name") == agent_name:
                mappings = agent.get("path_mappings", []) or []
                # Sort by host length descending (longest match first)
                for mapping in sorted(
                    mappings, key=lambda m: len(m.get("host", "") or ""), reverse=True
                ):
                    host = mapping.get("host", "") or ""
                    local = mapping.get("local", "") or ""
                    if host and host_path.startswith(host):
                        return host_path.replace(host, local, 1)
                break
        return host_path  # No mapping found, return as-is

    # ------------------------------------------------------------------
    # Health checks
    # ------------------------------------------------------------------

    async def ping_agent(self, name: str) -> bool:
        """Ping an agent to verify it is reachable."""
        if name not in self.agents:
            return False
        agent = self.agents[name]
        try:
            async with httpx.AsyncClient(timeout=5) as client:
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
        async with httpx.AsyncClient(timeout=timeout) as client:
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
        async with httpx.AsyncClient(timeout=timeout) as client:
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
        except Exception:
            return None

    async def get_container_stats(self, agent_name: str, container_id: str) -> Dict[str, Any]:
        """Return CPU/RAM stats for a container."""
        try:
            return await self._request(
                agent_name, "GET", f"/agent/containers/{container_id}/stats"
            )
        except Exception:
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
        except Exception:
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
        except Exception:
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
        except Exception:
            return False

    async def check_update(self, agent_name: str, container_id: str) -> Dict[str, Any]:
        """Check if a container image has an update available on the registry."""
        try:
            return await self._request(
                agent_name, "GET", f"/agent/containers/{container_id}/update-check"
            )
        except Exception:
            return {"update_available": False, "error": "Agent unreachable"}

    async def get_container_edit_spec(self, agent_name: str, container_id: str) -> Optional[Dict]:
        """Return the full spec of a container for editing."""
        try:
            return await self._request(
                agent_name, "GET", f"/agent/containers/{container_id}/edit-spec"
            )
        except Exception:
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

    async def get_stack_files(self, agent_name: str, stack_name: str) -> List[Dict[str, Any]]:
        """List files in a stack directory on an agent."""
        try:
            data = await self._request(
                agent_name, "GET", f"/agent/stacks/{stack_name}/files"
            )
            if isinstance(data, dict):
                return data.get("files", [])
            return []
        except Exception:
            return []

    async def get_stack_file(self, agent_name: str, stack_name: str, filename: str) -> Optional[str]:
        """Read a file from a stack directory on an agent."""
        try:
            return await self._request(
                agent_name, "GET", f"/agent/stacks/{stack_name}/files/{filename}"
            )
        except Exception:
            return None

    async def get_stack_files_with_content(self, agent_name: str, stack_name: str) -> dict:
        """List all files in a stack WITH their content in a single call.

        Returns a dict with a ``files`` key containing a list of
        ``{"filename": str, "content": str | None, "size": int}`` objects.
        """
        try:
            data = await self._request(
                agent_name, "GET", f"/agent/stacks/{stack_name}/files-with-content",
                timeout=30,
            )
            if isinstance(data, dict):
                return data
            return {"files": []}
        except Exception:
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
        except Exception:
            return []

    async def get_stack_version(self, agent_name: str, stack_name: str, hash: str) -> dict:
        """Return the content of a specific git version for a stack."""
        try:
            return await self._request(agent_name, "GET", f"/agent/stacks/{stack_name}/history/{hash}")
        except Exception:
            return None

    async def restore_stack_version(self, agent_name: str, stack_name: str, hash: str) -> dict:
        """Restore (checkout) a specific git version for a stack."""
        try:
            return await self._request(agent_name, "POST", f"/agent/stacks/{stack_name}/history/restore/{hash}")
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def update_git_history_settings(self, agent_name: str, settings: Dict[str, Any]) -> Dict[str, Any]:
        """Propagate git/history retention settings to an agent."""
        try:
            return await self._request(
                agent_name, "PUT", "/agent/settings/git-history", json=settings
            )
        except Exception as e:
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
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # Per-agent cache management
    # ------------------------------------------------------------------

    async def refresh_cache(self, agent_name: str):
        """Fetch containers, stacks and ports for an agent and cache them."""
        containers, stacks, ports = await asyncio.gather(
            self.get_containers(agent_name),
            self.get_stacks(agent_name),
            self.get_ports(agent_name),
            return_exceptions=True,
        )
        self.cache[agent_name] = {
            "containers": containers if isinstance(containers, list) else [],
            "stacks": stacks if isinstance(stacks, list) else [],
            "ports": ports if isinstance(ports, list) else [],
            "timestamp": time.time(),
        }

    async def invalidate_cache(self, agent_name: Optional[str] = None):
        """Force invalidation of the cache after an action on an agent.

        Removes the per-agent cache entry (or all of them when *agent_name* is
        ``None``), invalidates the aggregate stale-while-revalidate cache and
        rebuilds it immediately. A subsequent ``refreshStacks()`` / ``get_cached_*``
        call therefore returns the new state instead of stale-while-revalidate
        data.
        """
        if agent_name is not None:
            self.cache.pop(agent_name, None)
        else:
            self.cache.clear()
        # Invalidate the aggregate cache so the next read triggers a rebuild.
        for entry in self._cache.values():
            entry["data"] = None
            entry["timestamp"] = 0
            entry["pending"] = False
        try:
            await self._rebuild_aggregate_cache()
        except Exception as e:
            logger.warning("Cache rebuild after invalidation failed: %s", e)

    def _get_cached_containers(self, agent_name: str) -> List[Dict[str, Any]]:
        """Return cached containers for an agent, or an empty list."""
        return self.cache.get(agent_name, {}).get("containers", [])

    def _get_cached_stacks(self, agent_name: str) -> List[Dict[str, Any]]:
        """Return cached stacks for an agent, or an empty list."""
        return self.cache.get(agent_name, {}).get("stacks", [])

    def _get_cached_ports(self, agent_name: str) -> List[Dict[str, Any]]:
        """Return cached ports for an agent, or an empty list."""
        return self.cache.get(agent_name, {}).get("ports", [])

    # ------------------------------------------------------------------
    # Aggregate stale-while-revalidate cache (for "all" views)
    # ------------------------------------------------------------------

    def _get_cached_or_refresh(self, key: str, fetch_func) -> Optional[List[Dict[str, Any]]]:
        """Stale-while-revalidate: retourne le cache immédiatement, refresh en arrière-plan.

        Returns cached data (even if stale) or None if no cache exists yet.
        When stale data is returned, a background refresh is triggered.
        """
        cache = self._cache[key]
        now = time.time()

        # Cache frais (< 5s) → retour immédiat
        if cache["data"] is not None and now - cache["timestamp"] < 5:
            return cache["data"]

        # Cache périmé mais existant → retourne le cache + refresh en arrière-plan
        if cache["data"] is not None and not cache["pending"]:
            cache["pending"] = True
            loop = asyncio.get_event_loop()
            if loop and loop.is_running():
                asyncio.ensure_future(self._refresh_cache_entry(key, fetch_func))
            return cache["data"]

        # Pas de cache du tout → retourne None (le caller fera un fetch direct)
        return None

    async def _refresh_cache_entry(self, key: str, fetch_func):
        """Rafraîchit une entrée du cache en arrière-plan."""
        try:
            data = await fetch_func()
            self._cache[key]["data"] = data
            self._cache[key]["timestamp"] = time.time()
        except Exception as e:
            logger.error("Cache refresh failed for %s: %s", key, e)
        finally:
            self._cache[key]["pending"] = False

    async def start_background_refresh(self):
        """Event-driven refresh: full startup + WS events + sanity check."""
        logger.info("Starting event-driven refresh (no more 5s polling)")

        # 1. Full initial refresh
        await self.refresh_all_caches()

        # 2. Connect to each agent's event stream
        for name in self.agents:
            task = asyncio.create_task(self._connect_agent_events(name))
            self._ws_tasks[name] = task

        # 3. Sanity check every 10min
        async def _sanity_loop():
            while True:
                await asyncio.sleep(600)
                for name in list(self.agents.keys()):
                    if self.agents.get(name, {}).get("status") == "online":
                        try:
                            await self._incremental_refresh(name)
                        except Exception:
                            pass
        asyncio.create_task(_sanity_loop())

        # 4. Filet de sécurité: refresh périodique toutes les 60s
        async def _periodic_refresh():
            while True:
                await asyncio.sleep(60)
                try:
                    await self.refresh_all_caches()
                except Exception as e:
                    logger.warning("Periodic refresh failed: %s", e)

        asyncio.create_task(_periodic_refresh())

    # ------------------------------------------------------------------
    # Event-driven refresh (WebSocket events from agents)
    # ------------------------------------------------------------------

    async def _connect_agent_events(self, agent_name: str):
        """Connect WebSocket to agent's /agent/events with auto-reconnect."""
        while True:
            try:
                agent = self.agents.get(agent_name)
                if not agent:
                    await asyncio.sleep(10)
                    continue

                agent_url = agent.get("url", "").rstrip("/")
                api_key = agent.get("api_key", "")

                # Convert http → ws, https → wss
                ws_url = agent_url.replace("http://", "ws://").replace("https://", "wss://")
                ws_url += f"/agent/events?api_key={api_key}"

                import websockets as ws_lib
                async with ws_lib.connect(ws_url) as ws:
                    logger.info("Connected to agent '%s' events", agent_name)
                    if agent_name in self.agents:
                        self.agents[agent_name]["status"] = "online"

                    async for event in ws:
                        await self._handle_agent_event(agent_name, event)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Agent '%s' events disconnected: %s", agent_name, exc)
                if agent_name in self.agents:
                    self.agents[agent_name]["status"] = "offline"
                await asyncio.sleep(5)

    async def _handle_agent_event(self, agent_name: str, raw):
        """Process a single Docker event from an agent."""
        if isinstance(raw, (bytes, str)):
            try:
                import json
                raw = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            except Exception:
                return

        if not isinstance(raw, dict):
            return

        action = raw.get("Action", "")
        event_type = raw.get("Type", "")

        relevant_actions = {"start", "stop", "die", "kill", "pause", "unpause",
                            "restart", "create", "destroy", "rename"}

        if action in relevant_actions and event_type == "container":
            # Debounce: cancel previous timer for this agent
            if agent_name in self._event_debounce_timers:
                self._event_debounce_timers[agent_name].cancel()

            current_task: asyncio.Task | None = None

            async def _debounced():
                try:
                    await asyncio.sleep(2)
                    await self._incremental_refresh(agent_name)
                finally:
                    # Only remove the entry if it still belongs to this task.
                    # A new event may have already replaced it with a fresh task.
                    if self._event_debounce_timers.get(agent_name) is current_task:
                        self._event_debounce_timers.pop(agent_name, None)

            current_task = asyncio.create_task(_debounced())
            self._event_debounce_timers[agent_name] = current_task

            # Broadcast aux frontends
            try:
                from app.routes import api as api_routes
                for ws in list(api_routes._events_clients):
                    try:
                        await ws.send_json({"type": "docky_event", "agent": agent_name, "action": action})
                    except Exception:
                        pass
            except ImportError:
                pass

    async def _incremental_refresh(self, agent_name: str):
        """Refresh a single agent's data and rebuild aggregate caches."""
        try:
            await self.refresh_cache(agent_name)
            await self._rebuild_aggregate_cache()
        except Exception as e:
            logger.warning("Incremental refresh failed for '%s': %s", agent_name, e)

    async def _rebuild_aggregate_cache(self):
        """Rebuild aggregate cache from per-agent caches when possible.

        Falling back to the network only for agents whose cache is missing or
        stale avoids an extra N+1 request burst after every incremental refresh.
        """
        all_containers = []
        all_stacks = []
        all_ports = []

        for name in self.agents:
            cached = self.cache.get(name)
            if cached and (time.time() - cached.get("timestamp", 0)) < _AGENT_CACHE_TTL:
                c = cached.get("containers", [])
                s = cached.get("stacks", [])
                p = cached.get("ports", [])
            else:
                c, s, p = await asyncio.gather(
                    self.get_containers(name),
                    self.get_stacks(name),
                    self.get_ports(name),
                    return_exceptions=True,
                )
                c = c if isinstance(c, list) else []
                s = s if isinstance(s, list) else []
                p = p if isinstance(p, list) else []

            for container in c:
                if isinstance(container, dict):
                    container["agent_name"] = name
                    all_containers.append(container)

            for stack in s:
                if isinstance(stack, dict):
                    stack["agent_name"] = name
                    all_stacks.append(stack)

            for port in p:
                if isinstance(port, dict):
                    port["agent_name"] = name
                    all_ports.append(port)

        self._cache["containers"]["data"] = all_containers
        self._cache["containers"]["timestamp"] = time.time()
        self._cache["stacks"]["data"] = all_stacks
        self._cache["stacks"]["timestamp"] = time.time()
        self._cache["ports"]["data"] = all_ports
        self._cache["ports"]["timestamp"] = time.time()
        self._save_cache()

    async def ensure_cache(self, key: str) -> Optional[List[Dict[str, Any]]]:
        """Attend que le cache soit rempli s'il est vide (premier appel)."""
        cache = self._cache[key]
        if cache["data"] is not None:
            return cache["data"]

        if not cache["pending"]:
            if key == "containers":
                coro = self.get_all_containers()
            elif key == "stacks":
                coro = self.get_all_stacks()
            elif key == "ports":
                coro = self.get_all_ports()
            else:
                return None

            cache["pending"] = True
            try:
                data = await coro
                cache["data"] = data
                cache["timestamp"] = time.time()
                return data
            finally:
                cache["pending"] = False

        # Si déjà en refresh mais pas de données, le caller attendra un retry
        return None

    async def get_cached_containers(self) -> Optional[List[Dict[str, Any]]]:
        """Retourne les containers de tous les agents (cachés si possible).

        Utilise le stale-while-revalidate: retourne les données périmées
        immédiatement et rafraîchit en arrière-plan.
        """
        data = self._get_cached_or_refresh("containers", self.get_all_containers)
        if data is not None:
            return data
        # Cache vide (premier appel) → fetch direct et attend
        return await self.ensure_cache("containers")

    async def get_cached_stacks(self) -> Optional[List[Dict[str, Any]]]:
        """Retourne les stacks de tous les agents (cachées si possible)."""
        data = self._get_cached_or_refresh("stacks", self.get_all_stacks)
        if data is not None:
            return data
        return await self.ensure_cache("stacks")

    async def get_cached_ports(self) -> Optional[List[Dict[str, Any]]]:
        """Retourne les ports de tous les agents (cachés si possible)."""
        data = self._get_cached_or_refresh("ports", self.get_all_ports)
        if data is not None:
            return data
        return await self.ensure_cache("ports")

    async def refresh_all_caches(self):
        """Refresh per-agent caches and populate the aggregate cache.

        Called periodically by ``start_background_refresh`` or on demand.
        Each category is fetched independently so a single failure does not
        block the others.
        """
        # 1) Refresh per-agent caches (existing behaviour)
        names = [
            name
            for name, info in self.agents.items()
            if info["status"] in ("online", "unknown")
        ]
        tasks = [self.refresh_cache(name) for name in names]
        if tasks:
            await asyncio.gather(*tasks)

        # 2) Populate the aggregate cache
        try:
            containers = await self.get_all_containers()
            self._cache["containers"]["data"] = containers
            self._cache["containers"]["timestamp"] = time.time()
        except Exception as e:
            logger.warning("refresh_all_caches containers failed: %s", e)

        try:
            stacks = await self.get_all_stacks()
            self._cache["stacks"]["data"] = stacks
            self._cache["stacks"]["timestamp"] = time.time()
        except Exception as e:
            logger.warning("refresh_all_caches stacks failed: %s", e)

        try:
            ports = await self.get_all_ports()
            self._cache["ports"]["data"] = ports
            self._cache["ports"]["timestamp"] = time.time()
        except Exception as e:
            logger.warning("refresh_all_caches ports failed: %s", e)

        # 3) Persist cache to disk
        self._save_cache()

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