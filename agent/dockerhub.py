"""DEPRECATED — façade de rétrocompatibilité Docker Hub.

L'agent n'authentifiait historiquement que Docker Hub (ce module). Depuis la
généralisation multi-registres, l'implémentation vit dans
``agent/registries.py`` ; ce module n'est qu'une façade qui délègue vers
celle-ci (délégation dynamique, donc reste monkeypatchable via
``agent.registries``) :

- ``docker_login(username, token[, registry])`` → ``registries.docker_login``
  (registry vide → docker.io : comportement historique préservé) ;
- ``docker_logout([registry])`` → ``registries.docker_logout`` ;
- ``get_registry_auth([image])`` → ``registries.get_registry_auth`` (sans
  argument → credentials Docker Hub, comportement historique) ;
- constantes et helpers ré-exportés (``DOCKER_HUB_REGISTRY``,
  ``apply_persisted_config``, ``get_docker_config_dir``…).

Nouveau code : importer ``agent.registries`` et utiliser les endpoints
``POST /agent/registry/login``, ``POST /agent/registry/logout`` et
``GET /agent/registries``. Le endpoint legacy ``POST /agent/dockerhub/login``
(agent/routes.py) délègue déjà au nouveau mécanisme ; l'orchestrateur
(``app/agent_manager/client.py``) sera aligné dans le lot 2.
"""

from agent import registries
from agent.registries import (  # noqa: F401 — ré-exports rétrocompatibles
    DEFAULT_REGISTRY,
    DOCKER_HUB_REGISTRY,
    apply_persisted_config,
    get_config_json_path,
    get_docker_config_dir,
)


def docker_login(username: str, token: str, registry: str = ""):
    """Backward-compatible wrapper: ``(username, token)`` → registries.

    ``registry`` vide/None → Docker Hub (comportement historique). Voir
    :func:`agent.registries.docker_login` pour la sémantique complète.
    """
    return registries.docker_login(registry, username, token)


def docker_logout(registry: str = ""):
    """Backward-compatible wrapper → :func:`agent.registries.docker_logout`."""
    return registries.docker_logout(registry)


def get_registry_auth(image: str = None):
    """Backward-compatible wrapper → :func:`agent.registries.get_registry_auth`.

    Sans argument (appel historique) → credentials Docker Hub.
    """
    return registries.get_registry_auth(image)