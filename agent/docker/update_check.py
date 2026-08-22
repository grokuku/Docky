"""Update-check de registre Docker (extrait de docker_manager).

Vérifie si une image distante a changé sans puller, en combinant
``docker manifest inspect --verbose`` et le endpoint daemon
``/distribution/{name}/json`` (TTL cache). Toutes les fonctions de ce module
sont ré-exportées dans le namespace ``agent.docker_manager`` (façade).

Règle de la façade : toute dépendance susceptible d'être monkeypatchée par les
tests est résolue au moment de l'appel via ``_dm()`` (namespace docker_manager)
afin que les patches ``agent.docker_manager.<symbole>`` continuent de s'appliquer.
"""

import json
import logging
import subprocess
import time
from typing import Any, Dict, List

from docker.errors import DockerException, NotFound, APIError

logger = logging.getLogger(__name__)


def _dm():
    """Résolution tardive du namespace agent.docker_manager (évite tout cycle)."""
    from agent import docker_manager
    return docker_manager


# In-memory registry check cache: (repository, tag) -> {ts, digests, error}.
# The frontend checks updates in a loop; this avoids hitting the registry for
# every container on every refresh.
_UPDATE_CHECK_TTL = 300.0
_update_check_cache: Dict[tuple, Dict[str, Any]] = {}


def _split_image_reference(image_name: str) -> tuple:
    """Split an image reference into ``(repository, tag)``.

    Defaults to ``latest`` when no tag is present. Handles the common
    ``registry:port/repository:tag`` form by splitting on the *last* colon:

    - ``myimage`` / ``localhost:5000/myimage`` → ``('myimage', 'latest')``;
      the ``:port`` of a registry is NOT mistaken for a tag because the part
      after the last colon contains a ``/`` (a repo path, not a tag).
    - ``myimage:tag`` / ``localhost:5000/myimage:tag`` → explicit tag.
    - Digest references (``repo@sha256:...``) are returned as ``(ref, "")``.
    """
    image_name = (image_name or "").strip()
    if not image_name:
        return "", "latest"
    if "@" in image_name:
        return image_name, ""
    if ":" in image_name:
        repo, tag = image_name.rsplit(":", 1)
        if tag and "/" not in tag:
            return repo, tag
    return image_name, "latest"


def _canonical_repository(repository: str) -> str:
    """Normalize a repository name for update-check cache-key consistency.

    Docker accepts several spellings of the same image on Docker Hub
    (``nginx``, ``docker.io/nginx``, ``docker.io/library/nginx``), and the
    update/invalidate path may receive a different spelling than the one the
    check uses.  If they map to different cache keys, ``_invalidate_update_check``
    misses the entry the next ``check_image_update`` reads and the "update
    dispo" badge stays visible even after a successful update.

    Only Docker Hub short forms are collapsed; third-party registries
    (``ghcr.io/org/repo``, ``localhost:5000/repo``, ``registry:5000/repo``)
    and bare ``library/...`` namespaces are left untouched so private
    registries are never misinterpreted.
    """
    repository = (repository or "").strip()
    if repository.startswith("docker.io/library/"):
        return repository[len("docker.io/library/"):]
    if repository.startswith("docker.io/"):
        return repository[len("docker.io/"):]
    return repository


def _update_cache_key(repository: str, tag: str) -> tuple:
    """Build the canonical update-check cache key for ``repository:tag``.

    Both the check (:func:`_remote_manifest_digests`) and the invalidation
    (:func:`_invalidate_update_check`) MUST agree on this key, otherwise a
    stale remote-digest entry survives an update and the badge stays visible
    until the 300 s TTL expires (and, with a wrong-tag comparison, forever).
    """
    return (_canonical_repository(repository), tag)


def _update_check_cache_info(repository: str, tag: str) -> Dict[str, Any]:
    """Describe the cached remote-digest entry for ``repository:tag``.

    Used by the diagnostic log in :func:`check_image_update` so an operator
    can tell whether the result was served from cache (and how old) or absent
    (a fresh registry inspection was required).
    """
    entry = _update_check_cache.get(_update_cache_key(repository, tag))
    if entry is None:
        return {"cached": False}
    return {
        "cached": True,
        "age_s": round(time.time() - entry.get("ts", 0.0), 1),
        "error": entry.get("error"),
    }


def _clean_update_check_cache(now: float) -> None:
    """Remove expired entries from the in-memory update-check cache."""
    expired = [
        key for key, entry in _update_check_cache.items()
        if now - entry.get("ts", 0.0) >= _UPDATE_CHECK_TTL
    ]
    for key in expired:
        _update_check_cache.pop(key, None)


def _extract_remote_digests(payload: Any) -> List[str]:
    """Extract image manifest digests from ``docker manifest inspect`` JSON.

    Docker's verbose output format varies between versions:
    * a list of objects, each possibly with a ``Descriptor.digest``;
    * a single object with a ``Descriptor.digest``;
    * a manifest list with ``manifests[].digest``.
    """
    digests: List[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, list):
            for item in obj:
                walk(item)
            return
        if not isinstance(obj, dict):
            return

        descriptor = obj.get("Descriptor")
        if isinstance(descriptor, dict) and descriptor.get("digest"):
            digests.append(descriptor["digest"])

        for manifest in obj.get("manifests", []) or []:
            if isinstance(manifest, dict) and manifest.get("digest"):
                digests.append(manifest["digest"])
            else:
                walk(manifest)

        # Fallback for a bare manifest object that carries its own digest.
        if obj.get("digest") and isinstance(obj["digest"], str):
            digests.append(obj["digest"])

        # Recurse into nested objects (e.g. ``SchemaV2Manifest``) so
        # ``manifests`` is found regardless of the docker output format.
        # ``config`` and ``layers`` only contain blob digests, not image
        # manifest digests, so they are skipped.
        for key, value in obj.items():
            if key in ("Descriptor", "manifests", "config", "layers"):
                continue
            if isinstance(value, (dict, list)):
                walk(value)

    walk(payload)

    # De-duplicate while preserving order.
    return _dedupe_preserve_order(digests)


def _dedupe_preserve_order(items: List[str]) -> List[str]:
    """De-duplicate strings while preserving their first-seen order."""
    seen = set()
    unique: List[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def _short_digest(digest: Any) -> Any:
    """Truncate a ``sha256:...`` digest to ``sha256:`` + 12 hex chars."""
    if not digest:
        return digest
    text = str(digest)
    if text.startswith("sha256:"):
        return "sha256:" + text[7:19]
    return text[:19]


def _short_digests(digests: List[str]) -> List[str]:
    """Shorten a list of digests for readable logs (see :func:`_short_digest`)."""
    return [_short_digest(d) for d in (digests or [])]


def _remote_distribution_info(image_ref: str) -> Dict[str, Any]:
    """Query the daemon's ``/distribution/{name}/json`` endpoint.

    This performs the same registry lookup as ``docker manifest inspect`` and
    reports the digest of the manifest the tag actually points to — the
    top-level *index* digest for a multi-arch manifest list, or the image
    manifest digest for a single-arch image.  ``docker manifest inspect
    --verbose`` resolves a manifest list into its children and DROPS this
    digest, so it has to be fetched separately (see :func:`_remote_manifest_check`).

    For a digest-pinned reference (``name@sha256:...``) the pinned digest IS
    the top-level digest and no daemon call is needed.

    Returns a dict with ``digest``/``media_type``/``platforms`` keys (possibly
    empty) when the endpoint is unavailable — never raises.
    """
    info: Dict[str, Any] = {}
    if "@" in image_ref:
        pinned = image_ref.rsplit("@", 1)[-1]
        if pinned.startswith("sha256:"):
            info["digest"] = pinned
            return info
    try:
        client = _dm().get_docker_client()
        data = client.api.inspect_distribution(image_ref)
    except Exception as exc:  # noqa: BLE001 - diagnostics must never break the check
        logger.warning(
            "update-check distribution lookup failed image=%s error=%s",
            image_ref, exc,
        )
        return info
    if not isinstance(data, dict):
        return info
    descriptor = data.get("Descriptor")
    if isinstance(descriptor, dict):
        digest = descriptor.get("digest")
        if isinstance(digest, str) and digest:
            info["digest"] = digest
        media_type = descriptor.get("mediaType")
        if isinstance(media_type, str) and media_type:
            info["media_type"] = media_type
    platforms = data.get("Platforms")
    if isinstance(platforms, list):
        labels = []
        for platform in platforms:
            if isinstance(platform, dict):
                labels.append(
                    f"{platform.get('os') or '?'}/{platform.get('architecture') or '?'}"
                )
        if labels:
            info["platforms"] = labels
    return info


def _remote_manifest_check(repository: str, tag: str) -> Dict[str, Any]:
    """Return remote update-check data for ``repository:tag`` using a TTL cache.

    Combines two independent sources so BOTH image-store behaviours match:

    * ``docker manifest inspect --verbose`` (no image pull) yields the digest
      of every *child* manifest of the tag — one per platform, plus any
      cosign attestation manifests ghcr adds.  For a single-arch image the
      list has exactly one element (the image manifest itself).
    * The daemon's ``/distribution/{name}/json`` endpoint (:func:`_remote_distribution_info`)
      yields the *top-level* (index) digest — the digest a classic Docker
      image store records in ``RepoDigests`` for a multi-arch image.
      ``--verbose`` never emits it (the docker CLI resolves a manifest list
      into its children), so without it a multi-arch image on a classic store
      would ALWAYS look like an update (local index digest never matching the
      remote child digests).

    Returns a dict with ``digests`` (index + children, de-duplicated),
    ``index_digest``, ``child_digests``, ``media_type``, ``platforms`` and
    ``error``.  ``digests`` is empty when the registry is unreachable or the
    image is not found; update checks are deliberately non-blocking.
    """
    cache_key = _update_cache_key(repository, tag)
    now = time.time()
    cached = _update_check_cache.get(cache_key)
    if cached is not None and now - cached.get("ts", 0.0) < _UPDATE_CHECK_TTL:
        return cached

    _clean_update_check_cache(now)

    ref = repository if not tag else f"{repository}:{tag}"
    child_digests: List[str] = []
    error = None
    try:
        proc = subprocess.run(
            ["docker", "manifest", "inspect", "--verbose", ref],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode == 0:
            try:
                payload = json.loads(proc.stdout)
            except json.JSONDecodeError:
                payload = None
            child_digests = _extract_remote_digests(payload)
            if not child_digests:
                error = "No digest found in manifest inspect output"
        else:
            error = (proc.stderr or proc.stdout or "").strip()[:500]
    except (OSError, subprocess.SubprocessError) as exc:
        error = str(exc)

    # Merge the top-level (index) digest in front of the verbose child
    # digests so classic (index) and containerd (platform) stores both match.
    distribution = _remote_distribution_info(ref)
    index_digest = distribution.get("digest")
    digests = _dedupe_preserve_order(
        [d for d in (index_digest, *child_digests) if d]
    )

    info: Dict[str, Any] = {
        "ts": now,
        "digests": digests,
        "index_digest": index_digest,
        "child_digests": child_digests,
        "media_type": distribution.get("media_type"),
        "platforms": distribution.get("platforms", []),
        "error": error,
    }
    _update_check_cache[cache_key] = info
    return info


def _remote_manifest_digests(repository: str, tag: str) -> List[str]:
    """Return the remote digest list for ``repository:tag`` (TTL cached).

    Thin wrapper over :func:`_remote_manifest_check` kept for callers that only
    need the digest list.  Returns an empty list when the registry is
    unreachable or the image is not found; update checks are deliberately
    non-blocking.
    """
    return _dm()._remote_manifest_check(repository, tag).get("digests", [])


def _invalidate_update_check(image_ref: str) -> None:
    """Drop the cached remote digests for ``image_ref``.

    Called after a successful image update (pull + recreate).  The local
    image digest has just changed, but the 300 s TTL cache may still hold
    the *old* remote digests — captured before the manifest was refreshed
    on the registry.  Keeping them would make the next ``check_image_update``
    report ``update_available=True`` for an image that is actually up to
    date, leaving the "update dispo" badge visible for up to the TTL.
    """
    if not image_ref:
        return
    repository, tag = _split_image_reference(image_ref)
    key = _update_cache_key(repository, tag)
    if _update_check_cache.pop(key, None) is not None:
        logger.info("update-check cache invalidated image_ref=%s key=%r", image_ref, key)


def _invalidate_stack_update_cache(stack_name: str) -> int:
    """Invalidate the update-check cache for every registry image of a stack.

    Called after a successful ``docker compose pull`` + ``up -d`` (or a
    deploy): the local image digests have just changed, but the 300 s TTL
    cache may still hold the *old* remote digests captured before the
    manifests were refreshed on the registry. Keeping them would leave the
    "update dispo" badge visible for up to the TTL, exactly like the
    container bug this module already fixes via :func:`_invalidate_update_check`.

    Images are extracted from the stack's compose file the same way
    :func:`check_stack_update` reads it:

    - each service ``image:`` entry is used as-is (plain ``image:`` or
      ``build:`` + ``image:``);
    - services built locally with no ``image:`` (``build:`` only) are
      skipped — nothing is pulled from a registry, so their digest is
      unchanged;
    - ``${...}`` interpolated references are skipped because the stack
      environment (env file / shell) cannot be resolved here.

    Returns the number of cache keys invalidated.  When the compose file
    cannot be resolved (external stack whose file is not locatable) per-image
    invalidation is impossible and 0 is returned — the external stack badge
    is then corrected by the natural 300 s TTL expiration.
    """
    services = _dm()._compose_project_services(stack_name)
    if not services:
        return 0
    invalidated = 0
    for service in services.values():
        if not isinstance(service, dict):
            continue
        image = service.get("image")
        if not isinstance(image, str) or not image.strip():
            continue
        image_name = image.strip()
        if "${" in image_name:
            continue
        _invalidate_update_check(image_name)
        invalidated += 1
    return invalidated


def _local_repo_digests(image) -> List[str]:
    """Return the ``sha256:...`` digests from an image's ``RepoDigests``.

    An image can carry several ``RepoDigests`` entries (one per registry
    repo it was pulled from / retagged with, e.g. ``localhost:5000/repo@sha
    256:...`` and ``repo@sha256:...``).  ``update_available`` must be false
    as soon as ANY of them matches the remote manifest list, so callers test
    against the whole list instead of a single arbitrary entry.
    """
    try:
        digest_list = image.attrs.get("RepoDigests", []) or []
    except Exception as exc:
        logger.warning("_local_repo_digests: could not read RepoDigests: %s", exc)
        return []
    digests: List[str] = []
    seen = set()
    for item in digest_list:
        if not isinstance(item, str) or "@" not in item:
            continue
        digest = item.rsplit("@", 1)[-1]
        if digest.startswith("sha256:") and digest not in seen:
            seen.add(digest)
            digests.append(digest)
    return digests


def _local_repo_digests_for_image(image_name: str) -> List[str]:
    """Return ALL local repo digests for a pulled image, if available."""
    try:
        client = _dm().get_docker_client()
        image = client.images.get(image_name)
    except (NotFound, DockerException, APIError):
        return []
    return _local_repo_digests(image)


def check_image_update(container_id: str) -> Dict[str, Any]:
    """Check if a newer image is available on the registry for a container.

    Compares the local image digest with the remote registry digests obtained
    without pulling any image:

    * ``docker manifest inspect --verbose`` for the *child* digests (one per
      platform of a manifest list, plus any attestation manifests);
    * the daemon's ``/distribution/{name}/json`` endpoint for the *top-level*
      (index) digest, which the docker CLI's verbose output drops.  This is
      required because a classic Docker image store records the manifest-list
      (index) digest in ``RepoDigests``, while the containerd image store
      records the platform manifest digest — both must be matchable or a
      multi-arch image would be reported as "update available" forever.

    Returns a dict with ``update_available`` (bool), ``local_digest``,
    ``remote_digest``, ``local_tag`` and ``remote_tag``.

    The image reference used for BOTH the remote lookup and the cache key is
    the container's ``Config.Image`` — the exact reference it was created
    with — NOT ``image.tags[0]``.  For a multi-tag image the first tag can be
    a different one than the container actually runs (e.g. ``myapp:latest``
    while the container was created from ``myapp:1.0``): comparing against
    the wrong tag's manifest would report ``update_available=True`` forever
    even after a successful update, and the invalidation key (also derived
    from ``Config.Image``) would not match the key this check reads.
    """
    try:
        client = _dm().get_docker_client()
        c = client.containers.get(container_id)
        image = c.image
    except (NotFound, DockerException, APIError) as e:
        logger.warning("update-check container=%s unavailable: %s", container_id, e)
        return {
            "update_available": False,
            "image": None,
            "local_digest": None,
            "remote_digest": None,
            "local_tag": None,
            "remote_tag": None,
            "error": str(e),
        }

    # Authoritative reference: the exact image the container was created with.
    # image.tags[0] is deliberately NOT used (see docstring).
    config_image = (c.attrs.get("Config", {}).get("Image") or "").strip()
    image_name = config_image or (image.tags[0] if image.tags else None)
    if not image_name:
        return {
            "update_available": False,
            "image": None,
            "local_digest": None,
            "remote_digest": None,
            "local_tag": None,
            "remote_tag": None,
            "error": "No image tag found",
        }

    repository, tag = _split_image_reference(image_name)
    local_digests = _dm()._local_repo_digests(image)
    if not local_digests:
        logger.info(
            "update-check image=%s tag=%s local_digests=[] update_available=False "
            "reason=no_local_repo_digest (locally built image or registry "
            "without digests)",
            image_name, tag or "",
        )
        return {
            "update_available": False,
            "image": image_name,
            "local_digest": None,
            "remote_digest": None,
            "local_tag": tag or None,
            "remote_tag": tag or None,
            "error": "No local repo digest found",
        }

    cache_before = _update_check_cache_info(repository, tag)
    remote_info = _dm()._remote_manifest_check(repository, tag)
    remote_digests = remote_info.get("digests", [])
    remote_digest = remote_digests[0] if remote_digests else None

    # ``update_available`` is false as soon as ANY local repo digest appears
    # in the remote list.  The remote list merges the top-level (index) digest
    # with the verbose child digests so BOTH image stores match: classic store
    # (RepoDigests = manifest-list/index digest) and containerd store
    # (RepoDigests = platform manifest digest).  Extra attestation digests are
    # harmless — only a *missing* digest can produce a false positive.
    matched = bool(local_digests) and any(
        local_digest in remote_digests for local_digest in local_digests
    )
    if matched:
        update_available = False
        reason = "digests_match"
    elif not remote_digests:
        update_available = False
        reason = "no_remote_digests"
    elif not remote_info.get("index_digest"):
        # The top-level (index) digest could not be determined (e.g. the
        # daemon's /distribution endpoint failed).  With a classic image store
        # the local RepoDigest IS that index digest and would never appear in
        # the verbose child list, so a mismatch is not proof of an update.
        # Prefer a false negative over a permanent false positive.
        update_available = False
        reason = "remote_index_digest_unavailable"
    elif not remote_info.get("child_digests"):
        # The verbose child list is empty (unrecognised manifest format or
        # failed extraction), so a containerd-store local platform digest
        # cannot be confirmed either.  Again, prefer a false negative.
        update_available = False
        reason = "remote_child_digests_unavailable"
    else:
        update_available = True
        reason = "digest_mismatch"

    # Diagnostic line: if the badge ever stays visible, this log tells us the
    # exact image/tag/digests that were compared and whether the remote list
    # came from cache (and how old) or from a fresh registry inspection.
    logger.info(
        "update-check container=%s image=%s tag=%s local_digests=%s "
        "remote_digests=%s update_available=%s reason=%s cache_before=%s",
        container_id, image_name, tag or "", local_digests, remote_digests,
        update_available, reason, cache_before,
    )

    if update_available:
        # Unmissable WARNING marker for the operator: greppable as
        # ``UPDATE_CHECK_RESULT ... update_available=true`` with short digests.
        logger.warning(
            "UPDATE_CHECK_RESULT container=%s image=%s tag=%s "
            "local_digests=%s remote_digests=%s remote_index_digest=%s "
            "manifest_type=%s platforms=%s manifest_count=%s "
            "update_available=true reason=%s cache_before=%s",
            container_id, image_name, tag or "",
            _short_digests(local_digests), _short_digests(remote_digests),
            _short_digest(remote_info.get("index_digest")),
            remote_info.get("media_type") or "unknown",
            ",".join(remote_info.get("platforms") or []) or "unknown",
            len(remote_info.get("child_digests") or []),
            reason, cache_before,
        )

    return {
        "update_available": update_available,
        "image": image_name,
        "local_digest": local_digests[0],
        "remote_digest": remote_digest,
        "local_tag": tag or None,
        "remote_tag": tag or None,
    }
