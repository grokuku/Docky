"""LLM client and tool-calling system for Docky.

Provides:
- ``LLMClient``: async client for OpenAI-compatible chat completions (with
  streaming support).
- ``build_system_prompt``: assembles a system prompt embedding live Docker
  context (containers, stacks, ports) and the persistent ``soul.md`` memory.
- ``TOOLS``: OpenAI function-calling tool definitions for all Docker / stack
  / web / soul operations.
- ``execute_tool``: dispatches a tool call to the appropriate function.
- ``run_chat``: full agentic loop — call LLM, execute tools, feed results
  back, repeat until a final textual answer is produced.
- Firecrawl helpers: ``firecrawl_search``, ``firecrawl_scrape``,
  ``firecrawl_map``.
- Soul helpers: ``read_soul``, ``update_soul``.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.config import get_data_dir, load_settings
from app.docker_manager import client as docker

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


def build_system_prompt() -> str:
    """Build the system prompt with live Docker context and soul.md memory.

    Includes:
    1. Docky identity.
    2. Current container states.
    3. Current stacks.
    4. Used ports.
    5. Content of soul.md.
    6. Instructions about available tools.
    """
    parts: List[str] = []

    # 1. Identity
    parts.append(
        "Tu es Docky, un assistant de gestion de stacks Docker Compose. "
        "Tu aides l'utilisateur à gérer ses containers, créer et déployer des "
        "stacks, vérifier les ports, chercher des informations sur le web et "
        "maintenir une mémoire persistante. "
        "Tu réponds en français par défaut, de manière concise et utile."
    )

    # 2. Current containers
    try:
        containers = docker.list_containers(all=True)
        if containers:
            lines = []
            for c in containers:
                ports_str = ", ".join(
                    p.get("host_port", "") or p.get("container", "")
                    for p in c.get("ports", [])
                    if p
                ) or "aucun"
                lines.append(
                    f"  - {c['name']} ({c['id']}) | image: {c['image']} | "
                    f"status: {c['status']} | stack: {c.get('stack', '-')} | ports: {ports_str}"
                )
            parts.append("## Containers actuels\n" + "\n".join(lines))
        else:
            parts.append("## Containers actuels\nAucun container détecté.")
    except Exception as exc:
        parts.append(f"## Containers actuels\nErreur lors de la récupération: {exc}")

    # 3. Current stacks
    try:
        stacks = docker.list_stacks()
        if stacks:
            lines = []
            for s in stacks:
                lines.append(
                    f"  - {s['name']} (compose: {s.get('has_compose')}, env: {s.get('has_env')})"
                )
            parts.append("## Stacks disponibles\n" + "\n".join(lines))
        else:
            parts.append("## Stacks disponibles\nAucun stack trouvé.")
    except Exception as exc:
        parts.append(f"## Stacks disponibles\nErreur: {exc}")

    # 4. Used ports
    try:
        used_ports = docker.get_used_ports()
        if used_ports:
            port_lines = [
                f"  - {p['port']} ({p.get('source', '?')}"
                + (f", container: {p.get('container', '')}" if p.get("container") else "")
                + ")"
                for p in used_ports
            ]
            parts.append("## Ports utilisés\n" + "\n".join(port_lines))
        else:
            parts.append("## Ports utilisés\nAucun port en écoute détecté.")
    except Exception as exc:
        parts.append(f"## Ports utilisés\nErreur: {exc}")

    # 5. Soul.md
    soul = read_soul().strip()
    if soul:
        parts.append(f"## Mémoire persistante (soul.md)\n{soul}")
    else:
        parts.append("## Mémoire persistante (soul.md)\n(vide)")

    # 6. Tool instructions
    parts.append(
        "## Outils disponibles\n"
        "Tu peux utiliser les outils (function calls) suivants pour agir sur "
        "l'environnement Docker:\n"
        "- start_container / stop_container / restart_container\n"
        "- start_stack / stop_stack / restart_stack\n"
        "- get_container_logs\n"
        "- exec_in_container (⚠ nécessite validation humaine — propose la "
        "commande, ne l'exécute pas automatiquement)\n"
        "- create_stack / modify_stack_file / delete_stack / deploy_stack\n"
        "- set_file_permissions\n"
        "- get_used_ports / check_ports_available\n"
        "- web_search / web_scrape / web_map (via Firecrawl)\n"
        "- update_soul / read_soul\n\n"
        "Règles:\n"
        "- Avant de créer un stack ou d'exposer un port, vérifie toujours que "
        "les ports nécessaires sont disponibles avec check_ports_available.\n"
        "- Pour exec_in_container, NE l'exécute jamais toi-même. Retourne la "
        "commande proposée pour validation humaine.\n"
        "- Utilise update_soul pour mémiser des préférences ou informations "
        "importantes que l'utilisateur te demande de retenir.\n"
        "- Sois transparent: explique brièvement les actions que tu effectues."
    )

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Tool definitions (OpenAI function-calling format)
# ---------------------------------------------------------------------------

TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "start_container",
            "description": "Démarre un container Docker.",
            "parameters": {
                "type": "object",
                "properties": {
                    "container_id": {
                        "type": "string",
                        "description": "ID ou nom du container à démarrer",
                    }
                },
                "required": ["container_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_container",
            "description": "Arrête un container Docker.",
            "parameters": {
                "type": "object",
                "properties": {
                    "container_id": {
                        "type": "string",
                        "description": "ID ou nom du container à arrêter",
                    }
                },
                "required": ["container_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "restart_container",
            "description": "Redémarre un container Docker.",
            "parameters": {
                "type": "object",
                "properties": {
                    "container_id": {
                        "type": "string",
                        "description": "ID ou nom du container à redémarrer",
                    }
                },
                "required": ["container_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_stack",
            "description": "Démarre un stack Docker Compose (docker compose up -d).",
            "parameters": {
                "type": "object",
                "properties": {
                    "stack_name": {
                        "type": "string",
                        "description": "Nom du stack à démarrer",
                    }
                },
                "required": ["stack_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_stack",
            "description": "Arrête un stack Docker Compose (docker compose stop).",
            "parameters": {
                "type": "object",
                "properties": {
                    "stack_name": {
                        "type": "string",
                        "description": "Nom du stack à arrêter",
                    }
                },
                "required": ["stack_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "restart_stack",
            "description": "Redémarre un stack Docker Compose (docker compose restart).",
            "parameters": {
                "type": "object",
                "properties": {
                    "stack_name": {
                        "type": "string",
                        "description": "Nom du stack à redémarrer",
                    }
                },
                "required": ["stack_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_container_logs",
            "description": "Récupère les derniers logs d'un container.",
            "parameters": {
                "type": "object",
                "properties": {
                    "container_id": {
                        "type": "string",
                        "description": "ID ou nom du container",
                    },
                    "tail": {
                        "type": "integer",
                        "description": "Nombre de lignes de logs à récupérer (défaut: 100)",
                    },
                },
                "required": ["container_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "exec_in_container",
            "description": (
                "Exécute une commande dans un container. "
                "⚠ ATTENTION: cet outil nécessite une validation humaine. "
                "Le LLM doit proposer la commande mais elle ne sera pas exécutée "
                "automatiquement."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "container_id": {
                        "type": "string",
                        "description": "ID ou nom du container",
                    },
                    "command": {
                        "type": "string",
                        "description": "Commande shell à exécuter dans le container",
                    },
                },
                "required": ["container_id", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_stack",
            "description": "Crée un nouveau stack avec un docker-compose.yml et optionnellement un .env.",
            "parameters": {
                "type": "object",
                "properties": {
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
                "required": ["name", "compose_content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "modify_stack_file",
            "description": "Modifie ou crée un fichier dans un stack existant.",
            "parameters": {
                "type": "object",
                "properties": {
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
                "required": ["stack_name", "filename", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_stack",
            "description": "Supprime entièrement un stack et tous ses fichiers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "stack_name": {
                        "type": "string",
                        "description": "Nom du stack à supprimer",
                    }
                },
                "required": ["stack_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deploy_stack",
            "description": "Déploie un stack: docker compose down puis docker compose up -d.",
            "parameters": {
                "type": "object",
                "properties": {
                    "stack_name": {
                        "type": "string",
                        "description": "Nom du stack à déployer",
                    }
                },
                "required": ["stack_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_file_permissions",
            "description": "Définit les permissions (chmod) d'un fichier dans un stack.",
            "parameters": {
                "type": "object",
                "properties": {
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
                "required": ["stack_name", "filename", "mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_used_ports",
            "description": "Retourne la liste des ports actuellement en écoute sur l'hôte.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_ports_available",
            "description": "Vérifie si une liste de ports est disponible (non utilisés).",
            "parameters": {
                "type": "object",
                "properties": {
                    "ports": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Liste des ports à vérifier",
                    }
                },
                "required": ["ports"],
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


async def execute_tool(tool_name: str, arguments: Dict[str, Any]) -> str:
    """Execute a single tool call and return the result as a string.

    For ``exec_in_container``, a special marker is returned so the calling
    loop can add it to the ``needs_human_validation`` list instead of
    executing the command.
    """
    try:
        if tool_name == "start_container":
            ok = docker.start_container(arguments["container_id"])
            return "Container démarré." if ok else "Échec du démarrage du container."

        elif tool_name == "stop_container":
            ok = docker.stop_container(arguments["container_id"])
            return "Container arrêté." if ok else "Échec de l'arrêt du container."

        elif tool_name == "restart_container":
            ok = docker.restart_container(arguments["container_id"])
            return "Container redémarré." if ok else "Échec du redémarrage du container."

        elif tool_name == "start_stack":
            result = docker.compose_up(arguments["stack_name"])
            return _format_compose_result("Stack démarré", result)

        elif tool_name == "stop_stack":
            result = docker.compose_stop(arguments["stack_name"])
            return _format_compose_result("Stack arrêté", result)

        elif tool_name == "restart_stack":
            result = docker.compose_restart(arguments["stack_name"])
            return _format_compose_result("Stack redémarré", result)

        elif tool_name == "get_container_logs":
            tail = arguments.get("tail", 100)
            logs = docker.get_container_logs(arguments["container_id"], tail=tail)
            if not logs:
                return "Aucun log disponible."
            return "\n".join(logs)

        elif tool_name == "exec_in_container":
            # Do NOT execute — return marker for human validation
            container_id = arguments["container_id"]
            command = arguments["command"]
            return (
                f"{HUMAN_VALIDATION_MARKER}\n"
                f"Container: {container_id}\n"
                f"Command: {command}"
            )

        elif tool_name == "create_stack":
            name = arguments["name"]
            compose_content = arguments["compose_content"]
            env_content = arguments.get("env_content") or ""
            result = docker.create_stack(name, compose_content, env_content)
            return f"Stack '{name}' créé avec succès. Chemin: {result}"

        elif tool_name == "modify_stack_file":
            stack_name = arguments["stack_name"]
            filename = arguments["filename"]
            content = arguments["content"]
            docker.save_stack_file(stack_name, filename, content)
            return f"Fichier '{filename}' du stack '{stack_name}' mis à jour."

        elif tool_name == "delete_stack":
            stack_name = arguments["stack_name"]
            docker.delete_stack(stack_name)
            return f"Stack '{stack_name}' supprimé."

        elif tool_name == "deploy_stack":
            result = docker.deploy_stack(arguments["stack_name"])
            success = result.get("success", False)
            output = result.get("output", "")
            status = "déployé avec succès" if success else "échec du déploiement"
            text = f"Stack {status}."
            if output:
                text += f"\n--- output ---\n{output}"
            return text

        elif tool_name == "set_file_permissions":
            docker.set_file_permissions(
                arguments["stack_name"],
                arguments["filename"],
                arguments["mode"],
            )
            return f"Permissions de '{arguments['filename']}' définies sur {arguments['mode']}."

        elif tool_name == "get_used_ports":
            ports = docker.get_used_ports()
            if not ports:
                return "Aucun port en écoute détecté."
            lines = []
            for p in ports:
                extra = ""
                if p.get("container"):
                    extra = f" (container: {p['container']}, stack: {p.get('stack', '')})"
                lines.append(f"  {p['port']} [{p.get('source', '?')}]{extra}")
            return "Ports utilisés:\n" + "\n".join(lines)

        elif tool_name == "check_ports_available":
            port_list = arguments.get("ports", [])
            used = docker.get_used_ports()
            used_set = {str(p["port"]) for p in used}
            results = []
            for port in port_list:
                port_s = str(port)
                if port_s in used_set:
                    results.append(f"  Port {port}: ❌ déjà utilisé")
                else:
                    results.append(f"  Port {port}: ✅ disponible")
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

        else:
            return f"[error] Outil inconnu: {tool_name}"

    except FileNotFoundError as exc:
        return f"[error] {exc}"
    except FileExistsError as exc:
        return f"[error] {exc}"
    except ValueError as exc:
        return f"[error] {exc}"
    except Exception as exc:
        logger.exception("Unexpected error executing tool %s", tool_name)
        return f"[error] {type(exc).__name__}: {exc}"


def _format_compose_result(success_msg: str, result: Dict[str, Any]) -> str:
    """Format a compose command result dict into a readable string."""
    if result.get("success"):
        text = success_msg + "."
        output = result.get("output", "")
        if output:
            text += f"\n--- output ---\n{output.strip()}"
        return text
    else:
        error = result.get("error") or result.get("output", "")
        return f"Échec: {error}"


# ---------------------------------------------------------------------------
# Chat loop with tool calls
# ---------------------------------------------------------------------------

MAX_TOOL_ROUNDS = 10  # safety limit to avoid infinite loops


async def run_chat(
    user_message: str,
    conversation_history: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Run a full chat interaction with an agentic tool-calling loop.

    Steps:
    1. Build system prompt with live Docker context.
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
    system_prompt = build_system_prompt()
    messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    messages.extend(conversation_history)
    messages.append({"role": "user", "content": user_message})

    tool_calls_made: List[Dict[str, Any]] = []
    needs_human_validation: List[Dict[str, Any]] = []
    final_response = ""

    for round_idx in range(MAX_TOOL_ROUNDS):
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

                # Execute
                tool_result = await execute_tool(tool_name, tool_args)

                # Handle human-validation tools
                if tool_result.startswith(HUMAN_VALIDATION_MARKER):
                    needs_human_validation.append({
                        "name": tool_name,
                        "arguments": tool_args,
                        "id": tool_call_id,
                    })
                    # Tell the LLM that this command needs human validation
                    tool_result_msg = (
                        f"Cette commande nécessite une validation humaine avant exécution. "
                        f"Commande proposée: {tool_args.get('command', '')} "
                        f"sur le container {tool_args.get('container_id', '')}. "
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
        # Round limit reached
        if not final_response:
            final_response = (
                "J'ai atteint la limite d'interactions avec les outils. "
                "Voici ce que j'ai jusqu'à présent."
            )

    return {
        "response": final_response,
        "tool_calls_made": tool_calls_made,
        "needs_human_validation": needs_human_validation,
    }