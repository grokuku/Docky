"""Générateur d'événements Docker (extrait de docker_manager).

``watch_docker_events`` est ré-exporté dans le namespace
``agent.docker_manager`` (façade). ``get_docker_client`` (reste dans
docker_manager) est résolu via ``_dm()`` au moment de l'appel pour rester
compatible avec les monkeypatchs des tests.
"""

from typing import Any, Dict, Generator


def _dm():
    """Résolution tardive du namespace agent.docker_manager (évite tout cycle)."""
    from agent import docker_manager
    return docker_manager


def watch_docker_events() -> Generator[Dict[str, Any], None, None]:
    """Generate Docker events as they happen (blocking generator)."""
    client = _dm().get_docker_client()
    for event in client.events(decode=True):
        if isinstance(event, dict):
            yield event
