"""Multi-registry authentication for the Docky Agent.

Generalizes the former ``agent/dockerhub.py`` (Docker Hub only) to ANY docker
registry: Docker Hub, GHCR, GitLab, Quay, lscr.io, private/self-hosted…

All image pulls on the agent go through the Docker CLI (``docker compose
pull``, ``docker pull``) or, as a fallback, the docker-py SDK
(``client.images.pull``). The CLI reads its credentials from the config
directory pointed to by ``DOCKER_CONFIG`` (``config.json``), while the SDK
needs an explicit ``auth_config`` dict.

The docker CLI natively stores each registry's credentials under its own key
of ``<data_dir>/config.json`` (``auths`` mapping) — nothing to change on the
storage side for multi-registry support. This module therefore provides:

- :func:`docker_login` / :func:`docker_logout` — called by the
  ``POST /agent/registry/login`` and ``POST /agent/registry/logout``
  endpoints (and by the legacy ``POST /agent/dockerhub/login``, which
  delegates with the default registry, see ``agent/dockerhub.py``). The token
  is ALWAYS passed on **stdin** (``--password-stdin``), never in argv (it
  would leak into process listings / logs).
- :func:`list_stored_registries` — registry hosts that have credentials in
  the persisted ``config.json`` (normalized: ``https://index.docker.io/v1/``
  → ``docker.io``).
- :func:`scan_registries` — registry hosts actually used by the stacks'
  compose files (``image:`` fields), deduplicated and sorted.
- :func:`apply_persisted_config` — at agent startup, re-inject
  ``DOCKER_CONFIG=<data_dir>/.docker`` into ``os.environ`` if a persisted
  ``config.json`` exists, so every docker subprocess (compose/pull) inherits
  it and stays authenticated across restarts. The agent's data dir is a
  persistent volume, so the CLI config survives restarts.
- :func:`get_registry_auth` — pick, for a given image reference, the
  credentials of the registry that image belongs to and decode them into the
  ``auth_config`` dict expected by docker-py (used by the SDK pull fallbacks
  in ``docker_manager``, since SDK pulls do NOT read the CLI config).

The token is never logged: only success/failure and sanitized CLI errors are.
"""

import base64
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, List, Optional, Tuple

from agent.config import get_data_dir
from agent.docker.validation import get_stacks_dir

logger = logging.getLogger(__name__)

# Canonical host of the implicit default registry (Docker Hub).
DEFAULT_REGISTRY = "docker.io"

# Registry key written by `docker login` (no server argument) for Docker Hub
# in config.json — kept for backward compatibility with agent/dockerhub.py.
DOCKER_HUB_REGISTRY = "https://index.docker.io/v1/"

# Aliases of the Docker Hub registry host, all collapsed to DEFAULT_REGISTRY.
_DOCKER_IO_HOSTS = frozenset({
    "docker.io",
    "index.docker.io",
    "registry-1.docker.io",
    "registry.hub.docker.com",
    "hub.docker.com",
})

# Plausible registry host (safe to pass as a CLI argument): a hostname with
# optional port. Applied AFTER normalization (scheme/path already stripped).
_REGISTRY_HOST_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?(?::[0-9]{1,5})?$"
)

# Compose file names considered when scanning the stacks (same candidates as
# docker_manager.list_stacks / _compose_file_path).
COMPOSE_FILE_NAMES = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)


# ---------------------------------------------------------------------------
# Paths + startup persistence
# ---------------------------------------------------------------------------

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
    orchestrator during a previous run — one ``auths`` key per registry),
    ``DOCKER_CONFIG`` is set so that all docker subprocesses spawned later
    (``docker compose pull``, ``docker pull``… in ``docker_manager`` /
    ``compose_stream``) inherit it and stay authenticated after a restart.

    Returns ``True`` when a persisted config was found and applied.
    """
    config_path = get_config_json_path()
    if not config_path.exists():
        return False
    os.environ["DOCKER_CONFIG"] = str(get_docker_config_dir())
    logger.info(
        "Registres: configuration docker persistante détectée (%s) — "
        "DOCKER_CONFIG réinjecté pour les pulls authentifiés",
        config_path,
    )
    return True


# ---------------------------------------------------------------------------
# Host normalization / image → registry resolution
# ---------------------------------------------------------------------------

def normalize_registry_host(host: Optional[str]) -> str:
    """Normalize a registry host to its canonical form.

    - ``""``/``None`` → ``docker.io`` (the implicit default registry);
    - scheme and path are stripped (``https://index.docker.io/v1/`` →
      ``docker.io``);
    - the Docker Hub aliases (``index.docker.io``, ``registry-1.docker.io``…)
      are collapsed to ``docker.io``;
    - any other host is lower-cased and kept verbatim, port included
      (``registry.example.com:5000``).
    """
    host = (host or "").strip().lower()
    if not host:
        return DEFAULT_REGISTRY
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0].strip()
    if not host:
        return DEFAULT_REGISTRY
    if host in _DOCKER_IO_HOSTS:
        return DEFAULT_REGISTRY
    return host


def is_valid_registry_host(host: Optional[str]) -> bool:
    """Return ``True`` when *host* is a plausible registry host.

    Meant to be applied to an already-normalized host: rejects anything that
    should not be handed to ``docker login`` (spaces, ``/``, ``@``…). The
    default registry is always valid.
    """
    host = normalize_registry_host(host)
    return bool(host) and bool(_REGISTRY_HOST_RE.match(host))


def get_registry_of_image(image: Optional[str]) -> str:
    """Return the normalized registry host an image reference belongs to.

    Docker's rule: the registry host is the first path component (before the
    first ``/``) when it contains a ``.`` or a ``:`` (or is ``localhost``);
    otherwise the image lives on Docker Hub. Tags (``:tag``) and digests
    (``@sha256:…``) are stripped first so they cannot be mistaken for a host
    (``nginx:latest`` has no ``/`` → docker.io, not ``nginx:latest``).

    Examples::

        nginx                              → docker.io
        nginx:1.25 / nginx@sha256:…        → docker.io
        user/app                           → docker.io (Docker Hub namespace)
        ghcr.io/org/app:v1                 → ghcr.io
        localhost:5000/app:dev             → localhost:5000
        docker.io/library/nginx            → docker.io
    """
    ref = (image or "").strip()
    if not ref:
        return DEFAULT_REGISTRY
    ref = ref.split("@", 1)[0]
    if "/" not in ref:
        return DEFAULT_REGISTRY
    first = ref.split("/", 1)[0]
    if not first:
        return DEFAULT_REGISTRY
    if "." in first or ":" in first or first == "localhost":
        return normalize_registry_host(first)
    return DEFAULT_REGISTRY


# ---------------------------------------------------------------------------
# Login / logout (token on STDIN, never in argv)
# ---------------------------------------------------------------------------

def _display_name(host: str) -> str:
    """Human-facing registry name in messages ("Docker Hub" for the default)."""
    return "Docker Hub" if host == DEFAULT_REGISTRY else host


def docker_login(registry: str, username: str, token: str) -> Tuple[bool, str]:
    """Authenticate the docker CLI against *registry*.

    ``registry`` may be empty/``None`` → Docker Hub, with NO server argument
    (exactly the historical behavior: the CLI then writes the
    ``https://index.docker.io/v1/`` config key). Any other registry is passed
    as ``docker login <host>``.

    Runs ``docker --config <data_dir>/.docker login -u <username>
    --password-stdin [host]`` with the token written on **stdin** (NEVER in
    argv, so it cannot leak into ``ps`` output or command logs). On success
    the ``DOCKER_CONFIG`` environment variable is updated so subsequent docker
    subprocesses in this process immediately use the new credentials. The CLI
    stores the credentials under the registry's own ``auths`` key of the
    persisted ``config.json`` — other registries' credentials are preserved.

    Returns ``(success, message)``; ``message`` never contains the token.
    """
    host = normalize_registry_host(registry)
    config_dir = get_docker_config_dir()
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            "docker", "--config", str(config_dir),
            "login", "-u", username, "--password-stdin",
        ]
        if host != DEFAULT_REGISTRY:
            cmd.append(host)
        proc = subprocess.run(
            cmd,
            input=token.encode("utf-8"),
            capture_output=True,
            timeout=60,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("Registres: docker login %s a échoué: %s", host, exc)
        return False, f"docker login a échoué: {exc}"

    if proc.returncode == 0:
        # Subprocesses inherit os.environ: point them at the new config right
        # away (restart persistence is handled by apply_persisted_config).
        os.environ["DOCKER_CONFIG"] = str(config_dir)
        logger.info(
            "Registres: authentifié sur %s pour l'utilisateur '%s' (config: %s)",
            host, username, config_dir,
        )
        return True, f"Connecté à {_display_name(host)}"

    stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
    # Sanitize: never echo back anything that could contain the token.
    message = f"docker login a échoué: {stderr or 'erreur inconnue'}"
    logger.warning("Registres: %s", message)
    return False, message


def docker_logout(registry: str = "") -> Tuple[bool, str]:
    """Remove the credentials of *registry* from the agent.

    ``registry`` may be empty/None → Docker Hub. Runs ``docker --config
    <data_dir>/.docker logout [host]`` (best effort) then ALWAYS removes the
    matching ``auths`` entries from the persisted ``config.json`` (guards
    against a missing/failed CLI), because with multi-registry support the
    directory must NOT be blindly deleted: other registries' credentials may
    live next to it.

    The ``.docker`` directory is only removed when NO credentials remain on
    disk and no external credential store is configured (the historical
    full-cleanup behavior, preserved when Docker Hub is the only registry).

    Returns ``(success, message)``.
    """
    host = normalize_registry_host(registry)
    display = _display_name(host)
    config_dir = get_docker_config_dir()
    config_path = get_config_json_path()

    if not config_dir.exists():
        # Drop a stale DOCKER_CONFIG pointing at our (absent) config dir only;
        # never touch an externally-provided DOCKER_CONFIG.
        if os.environ.get("DOCKER_CONFIG") == str(config_dir):
            os.environ.pop("DOCKER_CONFIG", None)
        logger.info("Registres: logout %s (aucune config persistée)", host)
        return True, f"Déconnecté de {display}"

    # 1. Best-effort CLI logout (removes the entry + creds-store secrets).
    cli_ok = True
    try:
        cmd = ["docker", "--config", str(config_dir), "logout"]
        if host != DEFAULT_REGISTRY:
            cmd.append(host)
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
        cli_ok = proc.returncode == 0
        if not cli_ok:
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            logger.warning(
                "Registres: docker logout %s a échoué: %s",
                host, stderr or "erreur inconnue",
            )
    except (subprocess.SubprocessError, OSError) as exc:
        cli_ok = False
        logger.warning("Registres: docker logout %s a échoué: %s", host, exc)

    # 2. Surgical cleanup of config.json — removes THIS registry's entry only.
    _remove_auth_entries(config_path, host)

    message = f"Déconnecté de {display}"
    if not cli_ok:
        message += " (logout local uniquement)"

    # 3. No config.json at all (or nothing left in the dir) → remove the dir.
    if not config_path.exists():
        return _remove_config_dir(config_dir, message)

    remaining, has_store = _remaining_auth_state(config_path)
    if remaining is not None and not remaining and not has_store:
        # No credential left on disk, no external store → legacy full cleanup.
        removed = _remove_config_dir(config_dir, message)
        if removed[0] is False:
            return removed
        return True, message

    # Other registries are still authenticated: keep the config dir AND the
    # DOCKER_CONFIG export so subsequent pulls stay authenticated for them.
    logger.info(
        "Registres: identifiants %s supprimés — autres registres conservés (%s)",
        host, config_dir,
    )
    return True, message


def _remove_config_dir(config_dir: Path, message: str) -> Tuple[bool, str]:
    """Delete the docker config dir and drop our DOCKER_CONFIG if it points there."""
    try:
        shutil.rmtree(config_dir)
    except OSError as exc:
        logger.warning("Registres: suppression de %s impossible: %s", config_dir, exc)
        return False, f"Impossible de supprimer {config_dir}: {exc}"
    if os.environ.get("DOCKER_CONFIG") == str(config_dir):
        os.environ.pop("DOCKER_CONFIG", None)
    logger.info("Registres: configuration supprimée (%s)", config_dir)
    return True, message


def _remove_auth_entries(config_path: Path, target_host: str) -> int:
    """Drop the ``auths`` entries of *target_host* from the persisted config.

    Returns the number of removed entries (0 when none or file absent), or
    ``-1`` when the file cannot be parsed/rewritten (left untouched).
    """
    if not config_path.exists():
        return 0
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        auths = data.get("auths") if isinstance(data, dict) else None
        if not isinstance(auths, dict):
            return 0
        drop = [key for key in auths if normalize_registry_host(key) == target_host]
        if not drop:
            return 0
        for key in drop:
            del auths[key]
        data["auths"] = auths
        config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return len(drop)
    except Exception as exc:
        logger.warning("Registres: nettoyage de %s impossible: %s", config_path, exc)
        return -1


def _remaining_auth_state(config_path: Path) -> Tuple[Optional[dict], bool]:
    """Describe what is left in the persisted config.

    Returns ``(auths_dict, has_external_store)`` where ``auths_dict`` is the
    (possibly empty) remaining ``auths`` mapping, and ``has_external_store``
    tells whether a ``credsStore``/``credHelpers`` is configured (credentials
    then live OUTSIDE config.json). ``(None, False)`` when the file is
    unreadable — callers then stay conservative (no directory removal).
    """
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return None, False
    if not isinstance(data, dict):
        return None, False
    auths = data.get("auths")
    auths = auths if isinstance(auths, dict) else {}
    has_store = bool(data.get("credsStore")) or bool(data.get("credHelpers"))
    return auths, has_store


# ---------------------------------------------------------------------------
# Stored registries (config.json parsing)
# ---------------------------------------------------------------------------

def list_stored_registries() -> List[str]:
    """Return the registry hosts that have credentials in the persisted config.

    Parses ``<data_dir>/.docker/config.json`` (``auths`` mapping written by
    ``docker login``, one key per registry) and returns the hosts — normalized
    (``https://index.docker.io/v1/`` → ``docker.io``) — that hold either a
    basic ``auth`` blob or an ``identitytoken``, deduplicated and sorted.
    Credentials held in an external ``credsStore`` are not listed (the store
    is opaque from here).
    """
    config_path = get_config_json_path()
    if not config_path.exists():
        return []
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        auths = data.get("auths") if isinstance(data, dict) else None
    except Exception as exc:
        logger.warning("Registres: lecture de %s impossible: %s", config_path, exc)
        return []
    if not isinstance(auths, dict):
        return []
    hosts = set()
    for key, entry in auths.items():
        if not isinstance(entry, dict):
            continue
        if not (entry.get("auth") or entry.get("identitytoken")):
            continue
        hosts.add(normalize_registry_host(key))
    return sorted(hosts)


# ---------------------------------------------------------------------------
# Scan of the registries used by the stacks' composes
# ---------------------------------------------------------------------------

def scan_registries() -> List[str]:
    """Scan the stacks' compose files and return the registry hosts they use.

    For every stack directory under ``get_stacks_dir()`` (``/data/stacks``),
    parse each canonical compose file (``docker-compose.yml/yaml``,
    ``compose.yml/yaml`` — same candidates as ``docker_manager``) and extract
    the registry host of every ``image:`` field, applying docker's rule via
    :func:`get_registry_of_image` (first path component containing ``.``/``:``
    or ``localhost`` → that host; otherwise docker.io). Hosts are normalized
    and the result is deduplicated and sorted.

    Compose variable interpolation (``${REGISTRY}/app``) cannot be resolved
    without the stack environment and is skipped, like the update-check does.
    Unreadable/invalid compose files are skipped with a warning.
    """
    stacks_dir = get_stacks_dir()
    if not stacks_dir.is_dir():
        return []
    import yaml

    hosts = set()
    for entry in sorted(stacks_dir.iterdir()):
        if not entry.is_dir():
            continue
        for name in COMPOSE_FILE_NAMES:
            compose_file = entry / name
            if not compose_file.is_file():
                continue
            try:
                compose = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning(
                    "scan_registries: compose illisible %s: %s", compose_file, exc
                )
                continue
            if not isinstance(compose, dict):
                continue
            services = compose.get("services")
            if not isinstance(services, dict):
                continue
            for service in services.values():
                if not isinstance(service, dict):
                    continue
                image = service.get("image")
                if not isinstance(image, str):
                    continue
                image = image.strip()
                if not image or "${" in image:
                    continue
                hosts.add(get_registry_of_image(image))
    return sorted(hosts)


# ---------------------------------------------------------------------------
# SDK auth_config per image registry
# ---------------------------------------------------------------------------

def get_registry_auth(image: Optional[str] = None) -> Optional[dict]:
    """Return the docker-py ``auth_config`` for the registry of *image*.

    Decodes the ``auths.<registry>.auth`` entry (base64 ``username:token``)
    written by ``docker login`` in the persisted ``config.json`` for the
    registry the image belongs to (:func:`get_registry_of_image`): an image
    without registry prefix resolves to docker.io, ``ghcr.io/org/app`` to the
    ``ghcr.io`` credentials, etc. ``image=None``/empty (legacy behavior)
    resolves to Docker Hub.

    The docker-py SDK (``client.images.pull`` fallbacks in ``docker_manager``)
    does not read the CLI config, so pulls through the SDK need this explicit
    dict. Returns ``None`` when the image's registry has no usable stored
    credentials (anonymous pull).
    """
    config_path = get_config_json_path()
    if not config_path.exists():
        return None
    target = get_registry_of_image(image)
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        auths = data.get("auths") if isinstance(data, dict) else None
        if not isinstance(auths, dict):
            return None
        for key, entry in auths.items():
            if not isinstance(entry, dict):
                continue
            if normalize_registry_host(key) != target:
                continue
            auth_b64 = entry.get("auth")
            if auth_b64:
                decoded = base64.b64decode(auth_b64).decode("utf-8")
                username, _, password = decoded.partition(":")
                if username and password:
                    return {"username": username, "password": password}
            identity = entry.get("identitytoken")
            if identity:
                # Registry-token style credentials (docker-py auth_config form).
                return {"identitytoken": identity}
        return None
    except Exception as exc:
        logger.warning("Registres: lecture de %s impossible: %s", config_path, exc)
        return None