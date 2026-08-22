"""Parsing de métadonnées compose et construction du system prompt.

Extraites de ``app.llm.client``. Tous les symboles sont ré-exportés dans le
namespace ``app.llm.client`` (façade).

Règle de la façade : ``build_system_prompt`` dépend de ``agent_manager``, qui
est monkeypatché par les tests sur ``app.llm.client`` — il est donc résolu au
moment de l'appel via ``_client()`` (résolution tardive, aucun cycle d'import).
"""

import logging
import re
from typing import Any, Dict, List

from app.llm.soul import read_soul

logger = logging.getLogger(__name__)


def _client():
    """Résolution tardive du namespace app.llm.client (évite tout cycle)."""
    from app.llm import client
    return client


# ---------------------------------------------------------------------------
# Compose metadata parsing
# ---------------------------------------------------------------------------


def parse_compose_metadata(compose_content: str) -> dict:
    """Parse Docky metadata comments from a docker-compose.yml file.

    Extracts ``@key: value`` lines from the metadata comment block at the
    top of the file.  Returns an empty dict if no metadata is found.
    """
    metadata = {}
    pattern = r'#\s*@([\w]+):\s*(.+)'
    matches = re.findall(pattern, compose_content)
    for key, value in matches:
        metadata[key] = value.strip()
    return metadata


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
    7. Action rules (concise, direct).
    """
    # ``agent_manager`` est résolu dans le namespace de la façade au moment de
    # l'appel pour que les monkeypatchs ``app.llm.client.agent_manager`` des
    # tests continuent de s'appliquer.
    agent_manager = _client().agent_manager
    parts: List[str] = []

    # 1. Identity
    parts.append(
        "Tu es Docky, un assistant spécialisé dans la gestion de serveurs "
        "Docker multi-agents. Tu interagis avec plusieurs serveurs (agents) "
        "via des outils. Chaque action Docker nécessite un paramètre "
        "``agent_name``. Sois direct et concis."
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

                # Try to read compose metadata for richer context
                meta_str = ""
                try:
                    compose_content = await agent_manager.get_stack_file(
                        name, sname, "docker-compose.yml"
                    )
                    if compose_content:
                        meta = parse_compose_metadata(compose_content)
                        if meta:
                            parts_list = []
                            category = meta.get("category")
                            if category:
                                parts_list.append(f"[{category}]")
                            description = meta.get("description")
                            if description:
                                parts_list.append(description)
                            ports_meta = meta.get("ports")
                            if ports_meta:
                                parts_list.append(f"(ports: {ports_meta})")
                            hardware = meta.get("hardware")
                            if hardware:
                                parts_list.append(f"(hardware: {hardware})")
                            if parts_list:
                                meta_str = " - " + " ".join(parts_list)
                        else:
                            meta_str = " - (aucune métadonnée)"
                except Exception as exc:
                    logger.warning(
                        "Could not read compose metadata for stack '%s' on agent '%s': %s",
                        sname, name, exc,
                    )

                lines.append(
                    f"  - {sname} ({count} containers){extra_str}{meta_str}"
                )
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

    # 6b. Règles importantes pour les docker-compose
    parts.append(
        "## Règles importantes\n"
        "1. NE JAMAIS utiliser le champ `version:` dans les "
        "docker-compose.yml — il est déprécié.\n"
        "2. AVANT de créer ou modifier un docker-compose.yml, consulte la "
        "référence avec read_compose_reference si tu n'es pas sûr de la "
        "syntaxe.\n"
        "3. CHAQUE docker-compose.yml créé DOIT commencer par un bloc de "
        "métadonnées Docky (voir read_compose_reference pour le format).\n"
        "4. Choisir une catégorie pertinente pour chaque stack.\n"
        "5. Toujours utiliser `restart: unless-stopped` sauf raison "
        "spécifique.\n"
        "6. Utiliser le tag `latest` par défaut pour permettre les updates "
        "via docker compose pull. Utiliser un tag précis (ex: "
        "nginx:1.25) seulement si l'utilisateur demande une version "
        "spécifique ou si la stabilité est critique."
    )

    # 7. Règles d'action (concis, orienté action)
    parts.append(
        "## Règles\n"
        "1. **Agis directement** : quand on te demande de lister des containers, "
        "liste-les. N'explique pas les outils, utilise-les.\n"
        "2. **Utilise les outils** : pour toute action sur les serveurs, "
        "appelle le bon outil immédiatement.\n"
        "3. **Sois concis** dans tes réponses.\n"
        "4. **Spécifie toujours** le bon ``agent_name`` pour chaque action "
        "Docker.\n"
        "5. **Sécurité** : pour exec_in_container et clean_agent, ne les "
        "exécute jamais toi-même. Propose et attends la validation humaine.\n"
        "6. **Vérifie les ports** avant de créer un stack ou d'exposer un port.\n"
        "7. **Mémorise** avec update_soul ce que l'utilisateur demande de "
        "retenir.\n"
        "8. **Efficacité** : prépare le docker-compose.yml complet en une "
        "seule modification.\n"
        "9. **Réponds dans la langue de l'utilisateur**."
    )

    return "\n\n".join(parts)
