"""Single source of truth for the Docky Agent version.

Même contrat que ``orchestrator/app/version.py`` : les deux services sont des
images indépendantes sans package partagé, le helper est donc dupliqué
volontairement (aucune dépendance nouvelle). Résolution, par ordre de
priorité :

1. Variable d'environnement ``DOCKY_VERSION`` (si définie et non vide) ;
2. Fichier ``version.txt`` — racine du dépôt en checkout source,
   ``/app/version.txt`` dans le conteneur (écrit au build depuis
   ``ARG VERSION``, lui-même issu de ``version.txt`` par la CI) ;
3. Défaut sûr : :data:`DEFAULT_VERSION` (``"0.0.0"``) — jamais de crash au
   démarrage si le fichier est absent ou illisible.

Voir ``docs/versioning-unification.md`` pour la chaîne complète
version.txt → image Docker → runtime → API/UI.
"""

import os
from pathlib import Path

__all__ = [
    "DEFAULT_VERSION",
    "VERSION_ENV_VAR",
    "get_version",
]

#: Version affichée si aucune source n'est disponible.
DEFAULT_VERSION = "0.0.0"

#: Variable d'environnement prioritaire sur le fichier.
VERSION_ENV_VAR = "DOCKY_VERSION"

# .../agent → candidats : dossier service (/app en conteneur), puis parent
# (racine du dépôt en checkout source).
_MODULE_DIR = Path(__file__).resolve().parent
_CANDIDATE_DIRS = (_MODULE_DIR.parent, _MODULE_DIR.parent.parent)


def _read_version_file(path=None):
    """Read ``version.txt`` and return its stripped content, or ``None``.

    A missing, unreadable or whitespace-only file yields ``None`` so callers
    fall back to :data:`DEFAULT_VERSION` instead of raising.
    """
    if path is None:
        for directory in _CANDIDATE_DIRS:
            candidate = directory / "version.txt"
            if candidate.is_file():
                path = candidate
                break
        if path is None:
            return None
    try:
        content = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return content or None


def get_version(env_value=None, path=None) -> str:
    """Resolve the displayed Docky Agent version. Never raises.

    Priority: ``DOCKY_VERSION`` environment variable → ``version.txt``
    content → :data:`DEFAULT_VERSION`. Parameters exist for testability;
    production callers use the defaults.
    """
    if env_value is None:
        env_value = os.environ.get(VERSION_ENV_VAR)
    if env_value is not None:
        value = str(env_value).strip()
        if value:
            return value
    return _read_version_file(path) or DEFAULT_VERSION
