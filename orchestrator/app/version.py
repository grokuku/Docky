"""Single source of truth for the Docky orchestrator version.

Résolution (ordre de priorité) :

1. Variable d'environnement ``DOCKY_VERSION`` (si définie et non vide) ;
2. Fichier ``version.txt`` — racine du dépôt en checkout source,
   ``/app/version.txt`` dans le conteneur (écrit au build depuis
   ``ARG VERSION``, lui-même issu de ``version.txt`` par la CI) ;
3. Défaut sûr : :data:`DEFAULT_VERSION` (``"0.0.0"``) — jamais de crash au
   démarrage si le fichier est absent ou illisible.

Ce module est volontairement autonome (aucun import ``app.*``) afin d'être
utilisable depuis ``app/__init__.py`` sans cycle d'import. Voir
``docs/versioning-unification.md`` pour la chaîne complète
version.txt → image Docker → runtime → API/UI.
"""

import os
from pathlib import Path

__all__ = [
    "DEFAULT_VERSION",
    "VERSION_ENV_VAR",
    "_find_version_path",
    "_VERSION_PATH",
    "get_version",
]

#: Version affichée si aucune source n'est disponible.
DEFAULT_VERSION = "0.0.0"

#: Variable d'environnement prioritaire sur le fichier.
VERSION_ENV_VAR = "DOCKY_VERSION"

# .../orchestrator/app → candidats : dossier service (/app en conteneur),
# puis parent (racine du dépôt en checkout source).
_MODULE_DIR = Path(__file__).resolve().parent
_CANDIDATE_DIRS = (_MODULE_DIR.parent, _MODULE_DIR.parent.parent)


def _find_version_path() -> Path:
    """Return the most plausible path to ``version.txt``.

    In the Docker image the service root (``/app``) holds the file written at
    build time; in a source checkout only the repository root has one. Prefer
    whichever actually exists; when none does, return the first candidate so
    callers can fail gracefully via :func:`get_version`.
    """
    candidates = [directory / "version.txt" for directory in _CANDIDATE_DIRS]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


_VERSION_PATH = _find_version_path()


def _read_version_file(path=None):
    """Read ``version.txt`` and return its stripped content, or ``None``.

    A missing, unreadable or whitespace-only file yields ``None`` so callers
    fall back to :data:`DEFAULT_VERSION` instead of raising.
    """
    if path is None:
        path = _VERSION_PATH
    try:
        content = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return content or None


def get_version(env_value=None, path=None) -> str:
    """Resolve the displayed Docky version. Never raises.

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
