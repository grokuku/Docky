"""Tool definitions (OpenAI function-calling format) et exécuteur d'outils.

Extraites de ``app.llm.client``. Tous les symboles sont ré-exportés dans le
namespace ``app.llm.client`` (façade).

Règle de la façade : ``execute_tool`` dépend de ``agent_manager`` et des
helpers ``firecrawl_*``, monkeypatchés par les tests sur ``app.llm.client`` —
ils sont donc résolus au moment de l'appel via ``_client()`` (résolution
tardive, aucun cycle d'import).
"""

import json
import logging
from typing import Any, Dict, List

from app.llm.constants import HUMAN_VALIDATION_MARKER, _TOOLS_DOCKER_AGENT_PARAM
from app.llm.soul import read_soul, update_soul

logger = logging.getLogger(__name__)


def _client():
    """Résolution tardive du namespace app.llm.client (évite tout cycle)."""
    from app.llm import client
    return client


# ---------------------------------------------------------------------------
# Tool definitions (OpenAI function-calling format)
# ---------------------------------------------------------------------------

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
            "description": "Démarre un stack Docker Compose (docker compose up -d --remove-orphans, supprime les containers orphelins retirés du compose) sur un agent spécifique.",
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
            "description": "Déploie un stack (docker compose down puis up -d --remove-orphans) sur un agent spécifique.",
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
            "description": "Recherche sur le web via Firecrawl/WebClaw. Retourne des résultats pertinents.",
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
            "description": "Scrape le contenu d'une URL via Firecrawl/WebClaw.",
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
            "description": "Liste les URLs d'un site via Firecrawl/WebClaw (site map).",
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
            "name": "read_compose_reference",
            "description": "Lit la documentation de référence pour la création de docker-compose.yml. À consulter avant de créer ou modifier un compose pour utiliser la syntaxe à jour.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_stack",
            "description": "Met à jour une stack: docker compose pull (récupère les dernières images) puis docker compose up -d --remove-orphans (redémarre avec les nouvelles images et supprime les containers retirés du compose)",
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
# Tool executor
# ---------------------------------------------------------------------------

# Special marker returned for tools that require human validation.


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
    # ``agent_manager`` et les helpers ``firecrawl_*`` sont résolus dans le
    # namespace de la façade au moment de l'appel pour que les monkeypatchs
    # ``app.llm.client.<symbole>`` des tests continuent de s'appliquer.
    client = _client()
    agent_manager = client.agent_manager
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
            # Handle both old string format and new structured dict format
            if isinstance(logs, list) and logs and isinstance(logs[0], dict):
                return "\n".join(log["message"] for log in logs)
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
            result = await agent_manager.save_stack_file(
                agent_name, stack_name, filename, content
            )
            if isinstance(result, dict) and result.get("success"):
                return f"Fichier '{filename}' du stack '{stack_name}' (agent '{agent_name}') mis à jour."
            if isinstance(result, dict):
                return f"[error] Échec de la mise à jour du fichier '{filename}' sur l'agent '{agent_name}': {result.get('error', 'erreur inconnue')}"
            return f"[error] Échec de la mise à jour du fichier '{filename}' sur l'agent '{agent_name}'."

        elif tool_name == "get_stack_files":
            agent_name = arguments.get("agent_name")
            stack_name = arguments.get("stack_name")
            try:
                # L'outil LLM garde la vision complète du dossier (l'UI filtre
                # par défaut sur les fichiers éditables).
                result = await agent_manager.get_stack_files(agent_name, stack_name, include_hidden=True)
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
            return await client.firecrawl_search(query, limit=limit)

        elif tool_name == "web_scrape":
            return await client.firecrawl_scrape(arguments["url"])

        elif tool_name == "web_map":
            return await client.firecrawl_map(arguments["url"])

        elif tool_name == "update_soul":
            return update_soul(arguments["content"])

        elif tool_name == "read_soul":
            content = read_soul()
            return content if content else "soul.md est vide."

        elif tool_name == "read_compose_reference":
            try:
                from pathlib import Path
                from app.config import get_data_dir
                ref_path = Path(get_data_dir()) / "compose_reference.md"
                if ref_path.exists():
                    return ref_path.read_text(encoding='utf-8')
                else:
                    return json.dumps({"error": "Reference file not found"})
            except Exception as e:
                return json.dumps({"error": str(e)})

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
