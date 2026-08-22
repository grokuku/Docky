"""Path mapping helpers for agents.

Extracted from ``app.agent_manager.client``. ``translate_path`` is assigned
back onto ``AgentManager`` in the façade, so it keeps the exact same behaviour
(and the tests keep importing ``AgentManager`` from
``app.agent_manager.client`` unchanged).
"""

from app.config import load_settings


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
