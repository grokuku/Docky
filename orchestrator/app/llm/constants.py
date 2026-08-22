"""Constantes partagées du client LLM (extraites de ``app.llm.client``).

Toutes les constantes de ce module sont ré-exportées dans le namespace
``app.llm.client`` (façade) afin que les imports existants (routes, tests)
continuent de fonctionner à l'identique.
"""

# Special marker returned for tools that require human validation.
HUMAN_VALIDATION_MARKER = "__NEEDS_HUMAN_VALIDATION__"

# Safety limit to avoid infinite loops in the agentic chat loop.
MAX_TOOL_ROUNDS = 20

# Default Firecrawl/WebClaw endpoint (API /v1 compatible).
_DEFAULT_WEB_ENDPOINT = "https://api.firecrawl.dev/v1"

# Shared "agent_name" parameter used by every Docker-related tool definition.
_TOOLS_DOCKER_AGENT_PARAM = {
    "agent_name": {
        "type": "string",
        "description": "Nom de l'agent (serveur) sur lequel agir",
    }
}
