"""LLM client and tool-calling system for Docky.

Provides:
- ``LLMClient``: async client for OpenAI-compatible chat completions (with
  streaming support).
- ``build_system_prompt``: assembles a system prompt embedding live
  multi-agent Docker context (agents, containers, stacks, ports) and the
  persistent ``soul.md`` memory.
- ``TOOLS``: OpenAI function-calling tool definitions for all Docker / stack
  / web / soul operations.  Every Docker-related tool requires an
  ``agent_name`` parameter so the LLM can target a specific remote agent.
- ``execute_tool``: dispatches a tool call to the appropriate function via
  ``agent_manager``.
- ``run_chat``: full agentic loop — call LLM, execute tools, feed results
  back, repeat until a final textual answer is produced.
- Firecrawl helpers: ``firecrawl_search``, ``firecrawl_scrape``,
  ``firecrawl_map``.
- Soul helpers: ``read_soul``, ``update_soul``.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from app.config import get_data_dir, load_settings
from app.agent_manager.client import agent_manager

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
        firecrawl_cfg = settings.get("firecrawl", {}) or {}
        self.firecrawl_key: str = firecrawl_cfg.get("api_key") or ""

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
# Soul.md management
# ---------------------------------------------------------------------------


def _soul_path() -> Path:
    return Path(get_data_dir()) / "soul.md"


def read_soul() -> str:
    """Read and return the content of ``soul.md``."""
    path = _soul_path()
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def update_soul(content: str) -> str:
    """Overwrite ``soul.md`` with *content* and return a confirmation message."""
    path = _soul_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return "soul.md updated successfully."


# ---------------------------------------------------------------------------
# System prompt builder
# ---------------------------------------------------------------------------


def _format_container_ports(container: Dict[str, Any]) -> str:
    """Format the ports list of a container into a compact string."""
    ports = container.get("ports") or []
    host_ports: List[str] = []
    for p in ports:
        if not p:
            continue
        hp = p.get("host_port") or p.get("public_port") or p.get("container")
        if hp:
            host_ports.append(str(hp))
    return ", ".join(host_ports) if host_ports else "aucun"


async def build_system_prompt() -> str:
    """Build the system prompt with live multi-agent Docker context and
    ``soul.md`` memory.

    Includes:
    1. Docky identity.
    2. The list of configured agents with their online/offline status.
    3. Containers grouped by agent.
    4. Stacks grouped by agent.
    5. Used ports grouped by agent.
    6. Content of soul.md.
    7. Instructions about available tools.
    """
    parts: List[str] = []

    # 1. Identity
    parts.append(
        "Tu es Docky, un assistant de gestion de stacks Docker Compose "
        "multi-agents. Tu peux interagir avec plusieurs serveurs (agents) "
        "distant, chacun exécutant son propre Docker. Pour chaque action "
        "Docker, tu dois spécifier l'agent (serveur) ciblé via le paramètre "
        "``agent_name``. Tu aides l'utilisateur à gérer ses containers, créer "
        "et déployer des stacks, vérifier les ports, chercher des "
        "informations sur le web et maintenir une mémoire persistante. "
        "Tu réponds en français par défaut, de manière concise et utile."
    )

    # 2. Refresh agent statuses
    try:
        await agent_manager.ping_all()
    except Exception as exc:
        logger.warning("ping_all failed while building system prompt: %s", exc)

    agents = agent_manager.list_agents()

    # Agents disponibles
    if agents:
        agent_lines = []
        for a in agents:
            status = str(a.get("status", "unknown")).upper()
            agent_lines.append(f"- {a['name']} ({a['url']}) [{status}]")
        parts.append("## Agents disponibles\n" + "\n".join(agent_lines))
    else:
        parts.append("## Agents disponibles\nAucun agent configuré.")

    # 3. Fetch containers, stacks, ports across all agents
    try:
        all_containers = await agent_manager.get_all_containers()
    except Exception as exc:
        all_containers = []
        logger.warning("get_all_containers failed: %s", exc)
    try:
        all_stacks = await agent_manager.get_all_stacks()
    except Exception as exc:
        all_stacks = []
        logger.warning("get_all_stacks failed: %s", exc)
    try:
        all_ports = await agent_manager.get_all_ports()
    except Exception as exc:
        all_ports = []
        logger.warning("get_all_ports failed: %s", exc)

    # Containers grouped by agent
    for a in agents:
        name = a["name"]
        cts = [c for c in all_containers if c.get("agent_name") == name]
        if cts:
            lines = []
            for c in cts:
                cname = c.get("name") or c.get("id", "?")
                status = c.get("status", "?")
                image = c.get("image", "?")
                ports_str = _format_container_ports(c)
                stack = c.get("stack", "-")
                lines.append(
                    f"  - {cname} ({status}) - image: {image} - "
                    f"stack: {stack} - ports: {ports_str}"
                )
            parts.append(f"## Containers ({name})\n" + "\n".join(lines))
        else:
            parts.append(f"## Containers ({name})\nAucun container détecté.")

    # Stacks grouped by agent
    for a in agents:
        name = a["name"]
        stks = [s for s in all_stacks if s.get("agent_name") == name]
        if stks:
            lines = []
            for s in stks:
                sname = s.get("name", "?")
                # Compute container count from the containers list when possible
                count = sum(
                    1
                    for c in all_containers
                    if c.get("agent_name") == name and c.get("stack") == sname
                )
                extra = []
                if s.get("has_compose") is not None:
                    extra.append(f"compose: {s.get('has_compose')}")
                if s.get("has_env") is not None:
                    extra.append(f"env: {s.get('has_env')}")
                extra_str = f" - {', '.join(extra)}" if extra else ""
                lines.append(f"  - {sname} ({count} containers){extra_str}")
            parts.append(f"## Stacks ({name})\n" + "\n".join(lines))
        else:
            parts.append(f"## Stacks ({name})\nAucun stack trouvé.")

    # Ports grouped by agent
    for a in agents:
        name = a["name"]
        prts = [p for p in all_ports if p.get("agent_name") == name]
        if prts:
            port_lines = []
            for p in prts:
                port = p.get("port", "?")
                container = p.get("container") or p.get("source", "?")
                port_lines.append(f"  - {port} ({container})")
            parts.append(f"## Ports utilisés ({name})\n" + "\n".join(port_lines))
        else:
            parts.append(f"## Ports utilisés ({name})\nAucun port en écoute détecté.")

    # 6. Soul.md
    soul = read_soul().strip()
    if soul:
        parts.append(f"## Mémoire persistante (soul.md)\n{soul}")
    else:
        parts.append("## Mémoire persistante (soul.md)\n(vide)")

    # 7. Tool instructions
    parts.append(
        "## Outils disponibles\n"
        "Tu peux utiliser les outils (function calls) suivants pour agir sur "
        "l'environnement Docker. Tous les outils Docker nécessitent un "
        "paramètre ``agent_name`` indiquant le serveur (agent) ciblé:\n"
        "- start_container / stop_container / restart_container\n"
        "- start_stack / stop_stack / restart_stack\n"
        "- update_stack (pull + up -d pour mettre à jour les images)\n"
        "- get_container_logs\n"
        "- get_container_details / get_container_stats\n"
        "- list_containers (utilise 'all' pour tous les agents)\n"
        "- get_stack_status\n"
        "- get_agent_status (vérifie online/offline)\n"
        "- exec_in_container (⚠ nécessite validation humaine — propose la "
        "commande, ne l'exécute pas automatiquement)\n"
        "- clean_agent (⚠ nécessite validation humaine — docker system prune, "
        "action destructive)\n"
        "- create_stack / modify_stack_file / delete_stack / deploy_stack\n"
        "- get_stack_files / read_stack_file\n"
        "- set_file_permissions\n"
        "- get_used_ports / check_ports_available\n"
        "- web_search / web_scrape / web_map (via Firecrawl)\n"
        "- update_soul / read_soul\n\n"
        "Règles:\n"
        "- Spécifie toujours le bon ``agent_name`` pour chaque action Docker.\n"
        "- Avant de créer un stack ou d'exposer un port, vérifie toujours que "
        "les ports nécessaires sont disponibles avec check_ports_available.\n"
        "- Pour exec_in_container, NE l'exécute jamais toi-même. Retourne la "
        "commande proposée pour validation humaine.\n"
        "- Pour clean_agent, NE l'exécute jamais toi-même. C'est une action "
        "destructive qui nécessite validation humaine.\n"
        "- Utilise update_soul pour mémiser des préférences ou informations "
        "importantes que l'utilisateur te demande de retenir.\n"
        "- Sois transparent: explique brièvement les actions que tu effectues.\n"
        "- Tu peux lister les fichiers d'une stack avec get_stack_files et lire\n"
        "  le contenu d'un fichier avec read_stack_file. Utilise-les pour\n"
        "  diagnostiquer les problèmes de déploiement en lisant le\n"
        "  docker-compose.yml, le .env et les logs des containers.\n"
        "- IMPORTANT: Sois efficace avec les tool calls. Prépare le "
        "docker-compose.yml complet en une seule modification plutôt que de "
        "faire plusieurs modifications. Limite le nombre d'actions tool calls "
        "au strict nécessaire."
    )

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Tool definitions (OpenAI function-calling format)
# ---------------------------------------------------------------------------

_TOOLS_DOCKER_AGENT_PARAM = {
    "agent_name": {
        "type": "string",
        "description": "Nom de l'agent (serveur) sur lequel agir",
    }
}

TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "start_container",
            "description": "Démarre un container Docker sur un agent spécifique.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": _TOOLS_DOCKER_AGENT_PARAM["agent_name"],
                    "container_id": {
                        "type": "string",
                        "description": "ID ou nom du container à démarrer",
                    },
                },
                "required": ["agent_name", "container_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_container",
            "description": "Arrête un container Docker sur un agent spécifique.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": _TOOLS_DOCKER_AGENT_PARAM["agent_name"],
                    "container_id": {
                        "type": "string",
                        "description": "ID ou nom du container à arrêter",
                    },
                },
                "required": ["agent_name", "container_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "restart_container",
            "description": "Redémarre un container Docker sur un agent spécifique.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": _TOOLS_DOCKER_AGENT_PARAM["agent_name"],
                    "container_id": {
                        "type": "string",
                        "description": "ID ou nom du container à redémarrer",
                    },
                },
                "required": ["agent_name", "container_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_stack",
            "description": "Démarre un stack Docker Compose (docker compose up -d) sur un agent spécifique.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": _TOOLS_DOCKER_AGENT_PARAM["agent_name"],
                    "stack_name": {
                        "type": "string",
                        "description": "Nom du stack à démarrer",
                    },
                },
                "required": ["agent_name", "stack_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_stack",
            "description": "Arrête un stack Docker Compose (docker compose stop) sur un agent spécifique.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": _TOOLS_DOCKER_AGENT_PARAM["agent_name"],
                    "stack_name": {
                        "type": "string",
                        "description": "Nom du stack à arrêter",
                    },
                },
                "required": ["agent_name", "stack_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "restart_stack",
            "description": "Redémarre un stack Docker Compose (docker compose restart) sur un agent spécifique.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": _TOOLS_DOCKER_AGENT_PARAM["agent_name"],
                    "stack_name": {
                        "type": "string",
                        "description": "Nom du stack à redémarrer",
                    },
                },
                "required": ["agent_name", "stack_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_container_logs",
            "description": "Récupère les derniers logs d'un container sur un agent spécifique.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": _TOOLS_DOCKER_AGENT_PARAM["agent_name"],
                    "container_id": {
                        "type": "string",
                        "description": "ID ou nom du container",
                    },
                    "tail": {
                        "type": "integer",
                        "description": "Nombre de lignes de logs à récupérer (défaut: 100)",
                    },
                },
                "required": ["agent_name", "container_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "exec_in_container",
            "description": (
                "Exécute une commande dans un container sur un agent spécifique. "
                "⚠ ATTENTION: cet outil nécessite une validation humaine. "
                "Le LLM doit proposer la commande mais elle ne sera pas exécutée "
                "automatiquement."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": _TOOLS_DOCKER_AGENT_PARAM["agent_name"],
                    "container_id": {
                        "type": "string",
                        "description": "ID ou nom du container",
                    },
                    "command": {
                        "type": "string",
                        "description": "Commande shell à exécuter dans le container",
                    },
                },
                "required": ["agent_name", "container_id", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_stack",
            "description": "Crée un nouveau stack avec un docker-compose.yml et optionnellement un .env sur un agent spécifique.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": _TOOLS_DOCKER_AGENT_PARAM["agent_name"],
                    "name": {
                        "type": "string",
                        "description": "Nom du stack (alphanumérique, tirets, underscores)",
                    },
                    "compose_content": {
                        "type": "string",
                        "description": "Contenu complet du fichier docker-compose.yml",
                    },
                    "env_content": {
                        "type": "string",
                        "description": "Contenu optionnel du fichier .env",
                    },
                },
                "required": ["agent_name", "name", "compose_content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "modify_stack_file",
            "description": "Modifie ou crée un fichier dans un stack existant sur un agent spécifique.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": _TOOLS_DOCKER_AGENT_PARAM["agent_name"],
                    "stack_name": {
                        "type": "string",
                        "description": "Nom du stack",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Nom du fichier (ex: docker-compose.yml, .env)",
                    },
                    "content": {
                        "type": "string",
                        "description": "Nouveau contenu du fichier",
                    },
                },
                "required": ["agent_name", "stack_name", "filename", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_stack_files",
            "description": "Liste tous les fichiers présents dans le dossier d'une stack (docker-compose.yml, .env, fichiers de config, etc.)",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string", "description": "Nom de l'agent (serveur)"},
                    "stack_name": {"type": "string", "description": "Nom de la stack"}
                },
                "required": ["agent_name", "stack_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_stack_file",
            "description": "Lit le contenu d'un fichier dans le dossier d'une stack. Permet de voir le docker-compose.yml, le .env, ou n'importe quel fichier de config.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string", "description": "Nom de l'agent (serveur)"},
                    "stack_name": {"type": "string", "description": "Nom de la stack"},
                    "filename": {"type": "string", "description": "Nom du fichier à lire (ex: docker-compose.yml, .env)"}
                },
                "required": ["agent_name", "stack_name", "filename"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_stack",
            "description": "Supprime entièrement un stack et tous ses fichiers sur un agent spécifique.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": _TOOLS_DOCKER_AGENT_PARAM["agent_name"],
                    "stack_name": {
                        "type": "string",
                        "description": "Nom du stack à supprimer",
                    },
                },
                "required": ["agent_name", "stack_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deploy_stack",
            "description": "Déploie un stack (docker compose down puis up -d) sur un agent spécifique.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": _TOOLS_DOCKER_AGENT_PARAM["agent_name"],
                    "stack_name": {
                        "type": "string",
                        "description": "Nom du stack à déployer",
                    },
                },
                "required": ["agent_name", "stack_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_file_permissions",
            "description": "Définit les permissions (chmod) d'un fichier dans un stack sur un agent spécifique.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": _TOOLS_DOCKER_AGENT_PARAM["agent_name"],
                    "stack_name": {
                        "type": "string",
                        "description": "Nom du stack",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Nom du fichier",
                    },
                    "mode": {
                        "type": "string",
                        "description": "Permissions en octal (ex: 644, 755, 600)",
                    },
                },
                "required": ["agent_name", "stack_name", "filename", "mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_used_ports",
            "description": "Retourne la liste des ports actuellement en écoute sur l'hôte d'un agent spécifique.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": _TOOLS_DOCKER_AGENT_PARAM["agent_name"],
                },
                "required": ["agent_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_ports_available",
            "description": "Vérifie si une liste de ports est disponible (non utilisés) sur un agent spécifique.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": _TOOLS_DOCKER_AGENT_PARAM["agent_name"],
                    "ports": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Liste des ports à vérifier",
                    },
                },
                "required": ["agent_name", "ports"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Recherche sur le web via Firecrawl. Retourne des résultats pertinents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Requête de recherche",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Nombre maximum de résultats (défaut: 5)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_scrape",
            "description": "Scrape le contenu d'une URL via Firecrawl.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL à scraper",
                    }
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_map",
            "description": "Liste les URLs d'un site via Firecrawl (site map).",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL de base du site à mapper",
                    }
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_soul",
            "description": "Met à jour la mémoire persistante (soul.md) avec un nouveau contenu.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Nouveau contenu complet de soul.md",
                    }
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_soul",
            "description": "Lit et retourne le contenu actuel de la mémoire persistante (soul.md).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_stack",
            "description": "Met à jour une stack: docker compose pull (récupère les dernières images) puis docker compose up -d (redémarre avec les nouvelles images)",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string", "description": "Nom de l'agent (serveur)"},
                    "stack_name": {"type": "string", "description": "Nom de la stack à mettre à jour"}
                },
                "required": ["agent_name", "stack_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "clean_agent",
            "description": "Nettoie un agent: supprime les containers arrêtés, images orphelines, volumes inutilisés (docker system prune). ATTENTION: action destructive qui nécessite validation humaine.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string", "description": "Nom de l'agent (serveur) à nettoyer"}
                },
                "required": ["agent_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_container_details",
            "description": "Récupère les détails d'un container: image, ports, état, variables d'environnement, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string", "description": "Nom de l'agent"},
                    "container_id": {"type": "string", "description": "ID du container"}
                },
                "required": ["agent_name", "container_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_container_stats",
            "description": "Récupère les métriques temps réel d'un container: CPU%, mémoire utilisée, réseau I/O",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string", "description": "Nom de l'agent"},
                    "container_id": {"type": "string", "description": "ID du container"}
                },
                "required": ["agent_name", "container_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_stack_status",
            "description": "Récupère l'état d'une stack: liste des containers, leur état, ports exposés",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string", "description": "Nom de l'agent"},
                    "stack_name": {"type": "string", "description": "Nom de la stack"}
                },
                "required": ["agent_name", "stack_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_containers",
            "description": "Liste tous les containers d'un agent (running et stopped) avec leur état, image et ports",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string", "description": "Nom de l'agent. Utilise 'all' pour lister les containers de tous les agents."}
                },
                "required": ["agent_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_agent_status",
            "description": "Vérifie le statut d'un agent (online/offline) et retourne ses informations",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string", "description": "Nom de l'agent à vérifier"}
                },
                "required": ["agent_name"]
            }
        }
    },
]


# ---------------------------------------------------------------------------
# Firecrawl integration
# ---------------------------------------------------------------------------

FIRECRAWL_BASE = "https://api.firecrawl.dev/v1"


def _firecrawl_headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


async def firecrawl_search(query: str, limit: int = 5) -> str:
    """Search the web using the Firecrawl API.

    Returns a text summary of the results.
    """
    settings = load_settings()
    api_key = (settings.get("firecrawl", {}) or {}).get("api_key", "")
    if not api_key:
        return "[error] Firecrawl API key is not configured."

    url = f"{FIRECRAWL_BASE}/search"
    body = {"query": query, "limit": limit}
    try:
        async with httpx.AsyncClient(timeout=60.0) as http:
            resp = await http.post(url, json=body, headers=_firecrawl_headers(api_key))
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        return f"[error] Firecrawl search HTTP {exc.response.status_code}: {exc.response.text}"
    except httpx.RequestError as exc:
        return f"[error] Firecrawl search request error: {exc}"

    results = data.get("data") or data.get("results") or []
    if not results:
        return "Aucun résultat trouvé."

    lines = []
    for i, item in enumerate(results, 1):
        title = item.get("title") or item.get("metadata", {}).get("title", "")
        link = item.get("url") or item.get("link") or ""
        snippet = item.get("content") or item.get("snippet") or item.get("description", "")
        if snippet and len(snippet) > 500:
            snippet = snippet[:500] + "…"
        lines.append(f"{i}. {title}\n   URL: {link}\n   {snippet}")
    return "\n".join(lines)


async def firecrawl_scrape(url: str) -> str:
    """Scrape a URL using the Firecrawl API.

    Returns the page content as text.
    """
    settings = load_settings()
    api_key = (settings.get("firecrawl", {}) or {}).get("api_key", "")
    if not api_key:
        return "[error] Firecrawl API key is not configured."

    api_url = f"{FIRECRAWL_BASE}/scrape"
    body = {"url": url}
    try:
        async with httpx.AsyncClient(timeout=90.0) as http:
            resp = await http.post(api_url, json=body, headers=_firecrawl_headers(api_key))
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        return f"[error] Firecrawl scrape HTTP {exc.response.status_code}: {exc.response.text}"
    except httpx.RequestError as exc:
        return f"[error] Firecrawl scrape request error: {exc}"

    page_data = data.get("data") or data
    content = (
        page_data.get("markdown")
        or page_data.get("content")
        or page_data.get("html")
        or ""
    )
    if not content:
        return "Page scrapeée mais aucun contenu extrait."
    # Truncate very large pages
    if len(content) > 8000:
        content = content[:8000] + "\n\n… [contenu tronqué]"
    return content


async def firecrawl_map(url: str) -> str:
    """Map URLs on a site using the Firecrawl API.

    Returns a list of URLs as text.
    """
    settings = load_settings()
    api_key = (settings.get("firecrawl", {}) or {}).get("api_key", "")
    if not api_key:
        return "[error] Firecrawl API key is not configured."

    api_url = f"{FIRECRAWL_BASE}/map"
    body = {"url": url}
    try:
        async with httpx.AsyncClient(timeout=60.0) as http:
            resp = await http.post(api_url, json=body, headers=_firecrawl_headers(api_key))
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        return f"[error] Firecrawl map HTTP {exc.response.status_code}: {exc.response.text}"
    except httpx.RequestError as exc:
        return f"[error] Firecrawl map request error: {exc}"

    links = data.get("data") or data.get("links") or []
    if not links:
        return "Aucune URL trouvée."

    # links may be list of strings or list of dicts with 'url'
    url_list = []
    for item in links:
        if isinstance(item, str):
            url_list.append(item)
        elif isinstance(item, dict):
            u = item.get("url") or item.get("link", "")
            if u:
                url_list.append(u)

    if not url_list:
        return "Aucune URL trouvée."

    # Limit output
    if len(url_list) > 100:
        url_list = url_list[:100]
        return "\n".join(url_list) + f"\n\n… ({len(links)} URLs au total, 100 affichées)"
    return "\n".join(url_list)


# ---------------------------------------------------------------------------
# Tool executor
# ---------------------------------------------------------------------------

# Special marker returned for tools that require human validation.
HUMAN_VALIDATION_MARKER = "__NEEDS_HUMAN_VALIDATION__"


def _format_stack_result(success_msg: str, result: Dict[str, Any]) -> str:
    """Format a stack operation result dict into a readable string.

    The ``agent_manager`` returns dicts of the form ``{"success": bool,
    "output": str}`` or ``{"success": false, "error": str}``.
    """
    if isinstance(result, dict) and result.get("success"):
        text = success_msg + "."
        output = result.get("output", "")
        if output:
            text += f"\n--- output ---\n{output.strip()}"
        return text
    if isinstance(result, dict):
        error = result.get("error") or result.get("output", "")
        return f"Échec: {error}" if error else "Échec."
    if result:
        return success_msg + "."
    return "Échec."


async def execute_tool(tool_name: str, arguments: Dict[str, Any]) -> str:
    """Execute a single tool call and return the result as a string.

    Docker-related operations are delegated to ``agent_manager`` and require
    an ``agent_name`` argument.  For ``exec_in_container``, a special marker
    is returned so the calling loop can add it to the
    ``needs_human_validation`` list instead of executing the command.
    """
    try:
        if tool_name == "start_container":
            agent_name = arguments["agent_name"]
            ok = await agent_manager.start_container(agent_name, arguments["container_id"])
            return "Container démarré." if ok else "Échec du démarrage du container."

        elif tool_name == "stop_container":
            agent_name = arguments["agent_name"]
            ok = await agent_manager.stop_container(agent_name, arguments["container_id"])
            return "Container arrêté." if ok else "Échec de l'arrêt du container."

        elif tool_name == "restart_container":
            agent_name = arguments["agent_name"]
            ok = await agent_manager.restart_container(agent_name, arguments["container_id"])
            return "Container redémarré." if ok else "Échec du redémarrage du container."

        elif tool_name == "start_stack":
            agent_name = arguments["agent_name"]
            result = await agent_manager.start_stack(agent_name, arguments["stack_name"])
            return _format_stack_result("Stack démarré", result)

        elif tool_name == "stop_stack":
            agent_name = arguments["agent_name"]
            result = await agent_manager.stop_stack(agent_name, arguments["stack_name"])
            return _format_stack_result("Stack arrêté", result)

        elif tool_name == "restart_stack":
            agent_name = arguments["agent_name"]
            result = await agent_manager.restart_stack(agent_name, arguments["stack_name"])
            return _format_stack_result("Stack redémarré", result)

        elif tool_name == "get_container_logs":
            agent_name = arguments["agent_name"]
            tail = arguments.get("tail", 100)
            logs = await agent_manager.get_container_logs(
                agent_name, arguments["container_id"], tail=tail
            )
            if not logs:
                return "Aucun log disponible."
            return "\n".join(logs)

        elif tool_name == "exec_in_container":
            # Do NOT execute — return marker for human validation
            agent_name = arguments.get("agent_name", "?")
            container_id = arguments["container_id"]
            command = arguments["command"]
            return (
                f"{HUMAN_VALIDATION_MARKER}\n"
                f"Agent: {agent_name}\n"
                f"Container: {container_id}\n"
                f"Command: {command}"
            )

        elif tool_name == "create_stack":
            agent_name = arguments["agent_name"]
            name = arguments["name"]
            compose_content = arguments["compose_content"]
            env_content = arguments.get("env_content") or None
            result = await agent_manager.create_stack(
                agent_name, name, compose_content, env_content
            )
            if isinstance(result, dict) and result.get("success"):
                path = result.get("path", name)
                return f"Stack '{name}' créé avec succès sur l'agent '{agent_name}'. Chemin: {path}"
            if isinstance(result, dict):
                return f"[error] Échec de la création du stack: {result.get('error', 'erreur inconnue')}"
            return f"Stack '{name}' créé sur l'agent '{agent_name}'."

        elif tool_name == "modify_stack_file":
            agent_name = arguments["agent_name"]
            stack_name = arguments["stack_name"]
            filename = arguments["filename"]
            content = arguments["content"]
            ok = await agent_manager.save_stack_file(
                agent_name, stack_name, filename, content
            )
            if ok:
                return f"Fichier '{filename}' du stack '{stack_name}' (agent '{agent_name}') mis à jour."
            return f"[error] Échec de la mise à jour du fichier '{filename}' sur l'agent '{agent_name}'."

        elif tool_name == "get_stack_files":
            agent_name = arguments.get("agent_name")
            stack_name = arguments.get("stack_name")
            try:
                result = await agent_manager.get_stack_files(agent_name, stack_name)
                return json.dumps({"files": result})
            except Exception as e:
                return json.dumps({"error": str(e)})

        elif tool_name == "read_stack_file":
            agent_name = arguments.get("agent_name")
            stack_name = arguments.get("stack_name")
            filename = arguments.get("filename")
            try:
                result = await agent_manager.get_stack_file(agent_name, stack_name, filename)
                return json.dumps({"filename": filename, "content": result})
            except Exception as e:
                return json.dumps({"error": str(e)})

        elif tool_name == "delete_stack":
            agent_name = arguments["agent_name"]
            stack_name = arguments["stack_name"]
            result = await agent_manager.delete_stack(agent_name, stack_name)
            if isinstance(result, dict) and result.get("success"):
                return f"Stack '{stack_name}' supprimé de l'agent '{agent_name}'."
            if isinstance(result, dict):
                return f"[error] Échec de la suppression: {result.get('error', 'erreur inconnue')}"
            return f"Stack '{stack_name}' supprimé de l'agent '{agent_name}'."

        elif tool_name == "deploy_stack":
            agent_name = arguments["agent_name"]
            stack_name = arguments["stack_name"]
            result = await agent_manager.deploy_stack(agent_name, stack_name)
            success = result.get("success", False) if isinstance(result, dict) else False
            output = result.get("output", "") if isinstance(result, dict) else ""
            error = result.get("error", "") if isinstance(result, dict) else ""
            status = "déployé avec succès" if success else "échec du déploiement"
            text = f"Stack {status} sur l'agent '{agent_name}'."
            if output:
                text += f"\n--- output ---\n{output}"
            if not success and error:
                text += f"\n--- error ---\n{error}"
            return text

        elif tool_name == "set_file_permissions":
            agent_name = arguments["agent_name"]
            stack_name = arguments["stack_name"]
            filename = arguments["filename"]
            mode = arguments["mode"]
            result = await agent_manager.set_permissions(
                agent_name, stack_name, filename, mode
            )
            if isinstance(result, dict) and result.get("success"):
                return f"Permissions de '{filename}' (stack '{stack_name}', agent '{agent_name}') définies sur {mode}."
            if isinstance(result, dict):
                return f"[error] Échec chmod: {result.get('error', 'erreur inconnue')}"
            return f"Permissions de '{filename}' définies sur {mode}."

        elif tool_name == "get_used_ports":
            agent_name = arguments["agent_name"]
            ports = await agent_manager.get_ports(agent_name)
            if not ports:
                return f"Aucun port en écoute détecté sur l'agent '{agent_name}'."
            lines = []
            for p in ports:
                extra = ""
                if p.get("container"):
                    extra = f" (container: {p['container']}, stack: {p.get('stack', '')})"
                lines.append(f"  {p['port']} [{p.get('source', '?')}]{extra}")
            return f"Ports utilisés (agent '{agent_name}'):\n" + "\n".join(lines)

        elif tool_name == "check_ports_available":
            agent_name = arguments["agent_name"]
            port_list = arguments.get("ports", [])
            used = await agent_manager.get_ports(agent_name)
            used_set = {str(p["port"]) for p in used}
            results = []
            for port in port_list:
                port_s = str(port)
                if port_s in used_set:
                    results.append(f"  Port {port}: ❌ déjà utilisé (agent '{agent_name}')")
                else:
                    results.append(f"  Port {port}: ✅ disponible (agent '{agent_name}')")
            return "\n".join(results)

        elif tool_name == "web_search":
            query = arguments["query"]
            limit = arguments.get("limit", 5)
            return await firecrawl_search(query, limit=limit)

        elif tool_name == "web_scrape":
            return await firecrawl_scrape(arguments["url"])

        elif tool_name == "web_map":
            return await firecrawl_map(arguments["url"])

        elif tool_name == "update_soul":
            return update_soul(arguments["content"])

        elif tool_name == "read_soul":
            content = read_soul()
            return content if content else "soul.md est vide."

        elif tool_name == "update_stack":
            agent_name = arguments.get("agent_name")
            stack_name = arguments.get("stack_name")
            try:
                result = await agent_manager.update_stack(agent_name, stack_name)
                return json.dumps(result)
            except Exception as e:
                return json.dumps({"error": str(e)})

        elif tool_name == "clean_agent":
            # Do NOT execute — return marker for human validation
            agent_name = arguments.get("agent_name", "?")
            return (
                f"{HUMAN_VALIDATION_MARKER}\n"
                f"Type: clean_agent\n"
                f"Agent: {agent_name}\n"
                f"Command: docker system prune -f"
            )

        elif tool_name == "get_container_details":
            agent_name = arguments.get("agent_name")
            container_id = arguments.get("container_id")
            try:
                result = await agent_manager.get_container(agent_name, container_id)
                return json.dumps(result)
            except Exception as e:
                return json.dumps({"error": str(e)})

        elif tool_name == "get_container_stats":
            agent_name = arguments.get("agent_name")
            container_id = arguments.get("container_id")
            try:
                result = await agent_manager.get_container_stats(agent_name, container_id)
                return json.dumps(result)
            except Exception as e:
                return json.dumps({"error": str(e)})

        elif tool_name == "get_stack_status":
            agent_name = arguments.get("agent_name")
            stack_name = arguments.get("stack_name")
            try:
                stacks = await agent_manager.get_stacks(agent_name)
                containers = await agent_manager.get_containers(agent_name)
                stack_info = [s for s in stacks if s.get("name") == stack_name]
                stack_containers = [
                    c for c in containers
                    if c.get("stack") == stack_name
                    or stack_name in (c.get("names", [""]) if isinstance(c.get("names"), list) else [c.get("name", "")])
                ]
                return json.dumps({"stack": stack_info, "containers": stack_containers})
            except Exception as e:
                return json.dumps({"error": str(e)})

        elif tool_name == "list_containers":
            agent_name = arguments.get("agent_name")
            try:
                if agent_name == "all":
                    result = await agent_manager.get_all_containers()
                else:
                    result = await agent_manager.get_containers(agent_name)
                return json.dumps(result)
            except Exception as e:
                return json.dumps({"error": str(e)})

        elif tool_name == "get_agent_status":
            agent_name = arguments.get("agent_name")
            try:
                online = await agent_manager.ping_agent(agent_name)
                return json.dumps({
                    "agent_name": agent_name,
                    "online": online,
                    "url": agent_manager.agents.get(agent_name, {}).get("url", ""),
                })
            except Exception as e:
                return json.dumps({"error": str(e)})

        else:
            return f"[error] Outil inconnu: {tool_name}"

    except KeyError as exc:
        return f"[error] Argument manquant: {exc}"
    except FileNotFoundError as exc:
        return f"[error] {exc}"
    except FileExistsError as exc:
        return f"[error] {exc}"
    except ValueError as exc:
        return f"[error] {exc}"
    except Exception as exc:
        logger.exception("Unexpected error executing tool %s", tool_name)
        return f"[error] {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Chat loop with tool calls
# ---------------------------------------------------------------------------

MAX_TOOL_ROUNDS = 20  # safety limit to avoid infinite loops


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

    return {
        "response": final_response,
        "tool_calls_made": tool_calls_made,
        "needs_human_validation": needs_human_validation,
    }