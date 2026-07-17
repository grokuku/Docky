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
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from app.config import load_settings

logger = logging.getLogger(__name__)


class AgentManager:
    """Manage communication with one or more remote Docky agents."""

    def __init__(self):
        self.agents: Dict[str, Dict[str, Any]] = {}  # name -> {url, api_key, status, last_check}
        self.cache: Dict[str, Dict[str, Any]] = {}   # name -> {containers, stacks, ports, timestamp}
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
        self._load_agents()

    def list_agents(self) -> List[Dict[str, Any]]:
        """Return the list of agents with their current status."""
        return [
            {"name": name, "url": info["url"], "status": info["status"]}
            for name, info in self.agents.items()
        ]

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

    async def get_container_logs(self, agent_name: str, container_id: str, tail: int = 100) -> List[str]:
        """Return the last *tail* log lines of a container."""
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

    async def save_stack_file(self, agent_name: str, stack_name: str, filename: str, content: str) -> bool:
        """Write content to a file in a stack directory on an agent."""
        try:
            await self._request(
                agent_name, "PUT",
                f"/agent/stacks/{stack_name}/files/{filename}",
                content=content,
                headers={"Content-Type": "text/plain"},
            )
            return True
        except Exception:
            return False

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
            return await self._request(agent_name, "POST", "/agent/stacks", json=body)
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def delete_stack(self, agent_name: str, stack_name: str) -> Dict[str, Any]:
        """Delete a stack on an agent."""
        try:
            return await self._request(
                agent_name, "DELETE", f"/agent/stacks/{stack_name}"
            )
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def deploy_stack(self, agent_name: str, stack_name: str) -> Dict[str, Any]:
        """Deploy (down + up) a stack on an agent."""
        try:
            return await self._request(
                agent_name, "POST", f"/agent/stacks/{stack_name}/deploy", timeout=300
            )
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def start_stack(self, agent_name: str, stack_name: str) -> Dict[str, Any]:
        """Start (compose up) a stack on an agent."""
        try:
            return await self._request(
                agent_name, "POST", f"/agent/stacks/{stack_name}/start", timeout=300
            )
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def stop_stack(self, agent_name: str, stack_name: str) -> Dict[str, Any]:
        """Stop (compose stop) a stack on an agent."""
        try:
            return await self._request(
                agent_name, "POST", f"/agent/stacks/{stack_name}/stop", timeout=300
            )
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def restart_stack(self, agent_name: str, stack_name: str) -> Dict[str, Any]:
        """Restart (compose restart) a stack on an agent."""
        try:
            return await self._request(
                agent_name, "POST", f"/agent/stacks/{stack_name}/restart", timeout=300
            )
        except Exception as e:
            return {"success": False, "error": str(e)}

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

    # ------------------------------------------------------------------
    # Cache management
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

    async def refresh_all_caches(self):
        """Refresh the cache for every online agent in parallel."""
        names = [
            name
            for name, info in self.agents.items()
            if info["status"] in ("online", "unknown")
        ]
        tasks = [self.refresh_cache(name) for name in names]
        if tasks:
            await asyncio.gather(*tasks)

    def get_cached_containers(self, agent_name: str) -> List[Dict[str, Any]]:
        """Return cached containers for an agent, or an empty list."""
        return self.cache.get(agent_name, {}).get("containers", [])

    def get_cached_stacks(self, agent_name: str) -> List[Dict[str, Any]]:
        """Return cached stacks for an agent, or an empty list."""
        return self.cache.get(agent_name, {}).get("stacks", [])

    def get_cached_ports(self, agent_name: str) -> List[Dict[str, Any]]:
        """Return cached ports for an agent, or an empty list."""
        return self.cache.get(agent_name, {}).get("ports", [])

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