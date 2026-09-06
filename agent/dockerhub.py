"""Docker Hub authentication for the Docky Agent.

All image pulls on the agent go through the Docker CLI (``docker compose
pull``, ``docker pull``) or, as a fallback, the docker-py SDK
(``client.images.pull``). The CLI reads its credentials from the config
directory pointed to by ``DOCKER_CONFIG`` (``config.json``), while the SDK
needs an explicit ``auth_config`` dict.

This module therefore provides:

- :func:`docker_login` / :func:`docker_logout` — called by the
  ``POST /agent/dockerhub/login`` endpoint when the orchestrator pushes the
  Docker Hub credentials (see ``docs/dockerhub-auth.md``). The token is ALWAYS
  passed on **stdin** (``--password-stdin``), never in argv (it would leak
  into process listings / logs).
- :func:`apply_persisted_config` — at agent startup, re-inject
  ``DOCKER_CONFIG=<data_dir>/.docker`` into ``os.environ`` if a persisted
  ``config.json`` exists, so every docker subprocess (compose/pull) inherits
  it and stays authenticated across restarts. The agent's data dir is a
  persistent volume, so the CLI config survives restarts.
- :func:`get_registry_auth` — decode the persisted ``config.json`` into the
  ``auth_config`` dict expected by docker-py (used by the SDK pull fallbacks
  in ``docker_manager``), since SDK pulls do NOT read the CLI config.

The token is never logged: only success/failure and sanitized CLI errors are.
"""

import base64
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional, Tuple

from agent.config import get_data_dir

logger = logging.getLogger(__name__)

# Registry key written by `docker login` for Docker Hub in config.json.
DOCKER_HUB_REGISTRY = "https://index.docker.io/v1/"


def get_docker_config_dir() -> Path:
    """Return the persistent docker CLI config directory (<data_dir>/.docker).

    The agent's data dir is a mounted volume (see docker-compose.yml:
    ``./agent-data:/data``), so the config written there survives agent
    restarts.
    """
    return Path(get_data_dir()) / ".docker"


def get_config_json_path() -> Path:
    """Return the path of the docker CLI config.json in the data dir."""
    return get_docker_config_dir() / "config.json"


def apply_persisted_config() -> bool:
    """Re-inject ``DOCKER_CONFIG`` into the environment at agent startup.

    If ``<data_dir>/.docker/config.json`` exists (credentials pushed by the
    orchestrator during a previous run), ``DOCKER_CONFIG`` is set so that all
    docker subprocesses spawned later (``docker compose pull``, ``docker
    pull``… in ``docker_manager`` / ``compose_stream``) inherit it and stay
    authenticated after a restart.

    Returns ``True`` when a persisted config was found and applied.
    """
    config_path = get_config_json_path()
    if not config_path.exists():
        return False
    os.environ["DOCKER_CONFIG"] = str(get_docker_config_dir())
    logger.info(
        "Docker Hub: configuration docker persistante détectée (%s) — "
        "DOCKER_CONFIG réinjecté pour les pulls authentifiés",
        config_path,
    )
    return True


def docker_login(username: str, token: str) -> Tuple[bool, str]:
    """Authenticate the docker CLI against Docker Hub.

    Runs ``docker --config <data_dir>/.docker login -u <username>
    --password-stdin`` with the token written on **stdin** (NEVER in argv, so
    it cannot leak into ``ps`` output or command logs). On success the
    ``DOCKER_CONFIG`` environment variable is updated so subsequent docker
    subprocesses in this process immediately use the new credentials.

    Returns ``(success, message)``; ``message`` never contains the token.
    """
    config_dir = get_docker_config_dir()
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            "docker", "--config", str(config_dir),
            "login", "-u", username, "--password-stdin",
        ]
        proc = subprocess.run(
            cmd,
            input=token.encode("utf-8"),
            capture_output=True,
            timeout=60,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("Docker Hub: docker login a échoué: %s", exc)
        return False, f"docker login a échoué: {exc}"

    if proc.returncode == 0:
        # Subprocesses inherit os.environ: point them at the new config right
        # away (restart persistence is handled by apply_persisted_config).
        os.environ["DOCKER_CONFIG"] = str(config_dir)
        logger.info(
            "Docker Hub: authentifié pour l'utilisateur '%s' (config: %s)",
            username,
            config_dir,
        )
        return True, "Connecté à Docker Hub"

    stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
    # Sanitize: never echo back anything that could contain the token.
    message = f"docker login a échoué: {stderr or 'erreur inconnue'}"
    logger.warning("Docker Hub: %s", message)
    return False, message


def docker_logout() -> Tuple[bool, str]:
    """Remove the Docker Hub credentials from the agent.

    Runs ``docker --config <data_dir>/.docker logout`` (best effort) then
    ALWAYS deletes the ``.docker`` directory so no credential file is left on
    disk. Returns ``(success, message)``.
    """
    config_dir = get_docker_config_dir()
    message = "Déconnecté de Docker Hub"
    if config_dir.exists():
        try:
            subprocess.run(
                ["docker", "--config", str(config_dir), "logout"],
                capture_output=True,
                timeout=60,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            logger.warning("Docker Hub: docker logout a échoué: %s", exc)
            message = "Déconnecté de Docker Hub (logout local uniquement)"
        try:
            shutil.rmtree(config_dir)
        except OSError as exc:
            logger.warning("Docker Hub: suppression de %s impossible: %s", config_dir, exc)
            return False, f"Impossible de supprimer {config_dir}: {exc}"
    os.environ.pop("DOCKER_CONFIG", None)
    logger.info("Docker Hub: identifiants supprimés (%s)", config_dir)
    return True, message


def get_registry_auth() -> Optional[dict]:
    """Return the docker-py ``auth_config`` for Docker Hub, or ``None``.

    Decodes the ``auths.<registry>.auth`` (base64 ``username:token``) entry
    written by ``docker login`` in the persisted ``config.json``. The docker-py
    SDK (``client.images.pull`` fallbacks in ``docker_manager``) does not read
    the CLI config, so pulls through the SDK need this explicit dict.
    """
    config_path = get_config_json_path()
    if not config_path.exists():
        return None
    try:
        data: Any = json.loads(config_path.read_text(encoding="utf-8"))
        auth_b64 = (data.get("auths") or {}).get(DOCKER_HUB_REGISTRY, {}).get("auth")
        if not auth_b64:
            return None
        decoded = base64.b64decode(auth_b64).decode("utf-8")
        username, _, password = decoded.partition(":")
        if not username or not password:
            return None
        return {"username": username, "password": password}
    except Exception as exc:
        logger.warning("Docker Hub: lecture de %s impossible: %s", config_path, exc)
        return None