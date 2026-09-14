"""Integration façade Docky↔Homy (``/api/integration/v1``).

This module exposes a **dedicated, versioned, server-to-server REST surface**
for external integrations (first consumer: Homy). It is intentionally kept
separate from the browser API (``/api/*``, authenticated via the JWT session
cookie + CSRF double-submit):

- **Auth** — a single dedicated Bearer key (``security.integration_api_key``),
  compared in constant time (``hmac.compare_digest``). The key is independent
  from the MCP key (Q1) and can be viewed / copied / regenerated from the
  Settings UI (see ``app.routes.settings``).
- **CSRF** — every ``/api/integration/`` path is exempt from the double-submit
  check (``app.auth.csrf.API_EXEMPT_PREFIXES``): requests carry a Bearer header
  and no cookie/CSRF material, so mutations (batch health/stats, actions) must
  not be blocked.
- **Errors** — the façade always answers ``{error, code}`` (never the browser
  ``{detail}`` envelope).

Endpoints (LOT A)
-----------------

- ``GET  /agents``                                          → list agents (+version, lastCheck)
- ``GET  /agents/{agent}/containers``                       → normalized containers
- ``GET  /agents/{agent}/containers/{container}``           → one container (name OR id)
- ``POST /containers/health``                               → batch health check

Endpoints (LOT B)
-----------------

- ``GET  /agents/{agent}/containers/{container}/stats``     → single stats (0–100 host CPU)
- ``POST /containers/stats``                                → batch stats (grouped per agent)
- ``POST /agents/{agent}/containers/{container}/{action}``  → start/stop/restart

Stats decisions
---------------

- **CPU** — the agent reports a multi-core percentage; the façade exposes
  ``cpu_percent`` normalised to the **host 0–100** range (Q4) via
  :func:`normalize_cpu_percent`, plus ``cpu_percent_raw`` (agent value) and
  ``cpu_count`` (vCPU count) for transparency.
- **Memory** — ``mem_usage`` and ``mem_percent`` are both derived from the
  **same** quantity: ``cgroup usage - page cache`` (``inactive_file`` cgroup
  v2 / ``total_inactive_file`` cgroup v1), matching ``docker stats``. The
  deducted cache is exposed as ``mem_cache`` (Q6).
- **Disk** — not available through ``docker stats``; ``disk_usage``,
  ``disk_limit`` and ``disk_percent`` are always ``null``.

Normalisation (helpers in this module)
--------------------------------------

- ``state`` ∈ ``running|exited|paused|restarting|created|dead|unknown`` —
  unknown Docker labels (``removing``, …) map to ``"unknown"``.
- ``health`` ∈ ``healthy|unhealthy|starting|none`` — ``None`` → ``"none"``.
- ``checkedAt`` / ``lastCheck`` are UTC ISO-8601 (``...Z``).
- ``cpu_percent`` is normalised to the host 0–100 range (the agent reports a
  multi-core percentage; the façade divides by the vCPU count). See
  :func:`normalize_cpu_percent` (used by LOT B stats endpoints).

See ``docs/integration-api.md`` for the implemented contract and the Homy path
mapping.
"""

import asyncio
import contextlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.config import get_setting, load_settings, save_settings

logger = logging.getLogger(__name__)

#: Mounting prefix — the whole façade lives below this path.
INTEGRATION_PREFIX = "/api/integration/v1"

#: Maximum number of targets accepted by the batch health endpoint.
MAX_BATCH_TARGETS = 100
#: Maximum accepted request body size for the batch endpoint (256 KiB).
MAX_BODY_BYTES = 256 * 1024
#: Minimum length of a container-id prefix accepted for matching.
MIN_ID_PREFIX = 6

#: Agent-side stats request timeout (façade → agent). A one-shot ``docker
#: stats`` snapshot usually returns in ~1 s.
STATS_TIMEOUT = 15.0
#: Agent-side action timeout. Must be ≥ the agent's Docker stop timeout
#: (``stop_container`` uses ``timeout=10``) so a normal stop can finish,
#: while staying below the global 30 s request budget (LOT B contract).
ACTION_TIMEOUT = 20.0

#: Stats history windows (seconds) accepted by the façade — 15 min / 1 h / 24 h.
SUPPORTED_STATS_WINDOWS = (900, 3600, 86400)
#: A streamed (hot-set) sample younger than this is served directly by the
#: stats endpoints instead of paying a live ``docker stats`` round-trip.
HOT_SAMPLE_MAX_AGE = 2.0

#: Container states considered "stopped" for the ``stop`` idempotence check.
STOPPED_STATES = frozenset({"exited", "dead", "created"})

#: Canonical ``state`` values exposed by the façade (Homy contract).
VALID_STATES = frozenset(
    {"running", "exited", "paused", "restarting", "created", "dead", "unknown"}
)
#: Canonical ``health`` values exposed by the façade (Homy contract).
VALID_HEALTH = frozenset({"healthy", "unhealthy", "starting", "none"})

router = APIRouter(prefix=INTEGRATION_PREFIX)


# ---------------------------------------------------------------------------
# API key management (same model as the MCP key — see app.mcp_server)
# ---------------------------------------------------------------------------

def get_integration_api_key() -> str:
    """Return the integration Bearer key, generating and persisting one if absent.

    The key lives under ``security.integration_api_key`` in ``settings.yaml``
    and is **dedicated** to the integration façade (never the MCP key, Q1). A
    fresh cryptographically-random key is generated and saved on first demand
    so the Settings UI can always display one.
    """
    settings = load_settings()
    key = settings.get("security", {}).get("integration_api_key")
    if key:
        return key
    key = secrets.token_urlsafe(32)
    settings.setdefault("security", {})["integration_api_key"] = key
    save_settings(settings)
    logger.info("Generated a new integration API key (security.integration_api_key).")
    return key


def integration_auth_enabled() -> bool:
    """Whether the integration façade is served (default: True)."""
    return bool(get_setting("security.integration_enabled", True))


def _configured_integration_key() -> str:
    """Return the stored integration key **without** generating one.

    The façade must answer ``503 not_configured`` (rather than silently
    creating a key) when the administrator has not configured it yet. The key
    is created on first access to the Settings UI / regenerate endpoint.
    """
    return get_setting("security.integration_api_key", "") or ""


class IntegrationAuthError(Exception):
    """Raised by :func:`_check_integration_auth`; turned into a ``{error, code}`` body.

    Carrying the ready-to-serialise fields on the exception keeps the dependency
    tiny and lets :func:`integration_auth_exception_handler` (registered in
    ``app.main``) emit the façade's error envelope instead of FastAPI's default
    ``{"detail": ...}``.
    """

    def __init__(self, status_code: int, error: str, code: str):
        super().__init__(error)
        self.status_code = status_code
        self.error = error
        self.code = code


async def integration_auth_exception_handler(request: Request, exc: IntegrationAuthError) -> JSONResponse:
    """FastAPI exception handler rendering the façade error envelope."""
    return JSONResponse(status_code=exc.status_code, content={"error": exc.error, "code": exc.code})


async def _check_integration_auth(request: Request) -> None:
    """FastAPI dependency enforcing the dedicated Bearer key.

    Failure modes:

    - façade disabled or key not configured → ``503 {error, code:"not_configured"}``;
    - no ``Authorization`` header            → ``401 {error, code:"unauthorized"}``;
    - header present but not ``Bearer``      → ``403 {error, code:"forbidden"}``;
    - wrong Bearer token                     → ``401 {error, code:"unauthorized"}``.
    """
    if not integration_auth_enabled():
        raise IntegrationAuthError(503, "Docky is not configured", "not_configured")
    expected = _configured_integration_key()
    if not expected:
        raise IntegrationAuthError(503, "Docky is not configured", "not_configured")

    header = request.headers.get("Authorization", "")
    if not header:
        raise IntegrationAuthError(401, "Invalid or missing API key", "unauthorized")
    if not header.startswith("Bearer "):
        raise IntegrationAuthError(403, "Forbidden", "forbidden")
    token = header[len("Bearer "):].strip()
    try:
        valid = hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))
    except Exception:
        valid = False
    if not valid:
        raise IntegrationAuthError(401, "Invalid or missing API key", "unauthorized")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _api():
    """Résolution tardive du namespace app.routes.api (évite tout cycle).

    Keeps the tests' ``app.routes.api.agent_manager`` monkeypatch effective.
    """
    from app.routes import api
    return api


def _manager():
    """Return the (late-resolved) agent manager singleton."""
    return _api().agent_manager


def _stats_manager():
    """Return the (late-resolved) real-time stats stream manager.

    Indirection monkeypatchable by tests (``app.routes.integration.
    _stats_manager``) so the hot cache can be driven without any network.
    """
    from app.agent_manager import stats_stream
    return stats_stream.get_manager()


def _error(status_code: int, error: str, code: str) -> JSONResponse:
    """Build a façade error response (``{error, code}``, never ``{detail}``)."""
    return JSONResponse(status_code=status_code, content={"error": error, "code": code})


def utc_iso_now() -> str:
    """Return the current UTC time as ISO-8601 with a ``Z`` suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_from_timestamp(timestamp: Any) -> Optional[str]:
    """Convert a Unix timestamp to ISO-8601 UTC (``None`` when unset/invalid)."""
    if not timestamp:
        return None
    try:
        return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def normalize_state(value: Any) -> str:
    """Normalise a Docker state label to the façade enumeration.

    Unrecognised labels (``removing``, ``stopping``, …) and empty values map
    to the safe default ``"unknown"``.
    """
    if not value:
        return "unknown"
    state = str(value).strip().lower()
    return state if state in VALID_STATES else "unknown"


def normalize_health(value: Any) -> str:
    """Normalise a Docker health label to the façade enumeration.

    ``None`` (no healthcheck configured) maps to ``"none"``; any unrecognised
    value also maps to ``"none"``.
    """
    if value is None:
        return "none"
    health = str(value).strip().lower()
    return health if health in VALID_HEALTH else "none"


def normalize_cpu_percent(raw: Any, cpu_count: Any) -> float:
    """Normalise an agent CPU percentage to the host 0–100 range (Q4).

    The agent reports ``(cpu_delta / system_delta) * cpu_count * 100`` (a
    multi-core percentage, e.g. 0–800 %). The façade divides by the vCPU count
    to expose the 0–100 host utilisation, clamped for safety.
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    try:
        count = int(cpu_count)
    except (TypeError, ValueError):
        count = 0
    if count <= 0:
        count = 1
    value = value / count
    value = max(0.0, min(100.0, value))
    return round(value, 2)


def normalize_container(container: Any) -> Dict[str, Any]:
    """Return a façade-normalised container dict.

    Shape: ``{id, name, image, state, health, stack, service}``. Tolerates the
    agent's legacy ``Id`` key and missing fields so the façade never 500s on a
    partial payload.
    """
    if not isinstance(container, dict):
        return {
            "id": "",
            "name": "",
            "image": "",
            "state": "unknown",
            "health": "none",
            "stack": None,
            "service": "",
        }
    stack = container.get("stack")
    return {
        "id": container.get("id") or container.get("Id") or "",
        "name": container.get("name") or "",
        "image": container.get("image") or "",
        "state": normalize_state(container.get("state") or container.get("status")),
        "health": normalize_health(container.get("health")),
        "stack": stack if stack else None,
        "service": container.get("service") or "",
    }


def _match_container(containers: List[Dict[str, Any]], needle: str) -> Optional[Dict[str, Any]]:
    """Match a container by exact name, exact id, or id prefix (Docker semantics)."""
    if not needle:
        return None
    for container in containers:
        if not isinstance(container, dict):
            continue
        cid = container.get("id") or container.get("Id") or ""
        if needle == cid or needle == (container.get("name") or ""):
            return container
    if len(needle) >= MIN_ID_PREFIX:
        for container in containers:
            if not isinstance(container, dict):
                continue
            cid = container.get("id") or container.get("Id") or ""
            if cid and cid.startswith(needle):
                return container
    return None


def _result_error(agent: str, container: str, code: str, message: str) -> Dict[str, Any]:
    """Build a per-target batch error result (always ``found:false``)."""
    return {
        "agent": agent,
        "container": container,
        "found": False,
        "state": "unknown",
        "error": {"code": code, "message": message},
    }


# ---------------------------------------------------------------------------
# Stats helpers (LOT B)
# ---------------------------------------------------------------------------

def _as_int(value: Any, default: int = 0) -> int:
    """Coerce *value* to ``int`` (never raises); fall back to *default*."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    """Coerce *value* to ``float`` (never raises); fall back to *default*."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _zero_stats_payload() -> Dict[str, Any]:
    """Return the zeroed stats block shared by error/stopped results.

    Mirrors :func:`_stats_payload` exactly (same keys) so a batch consumer can
    read every field regardless of the per-target outcome. ``disk_*`` are
    always ``null`` (not available through ``docker stats``).
    """
    return {
        "state": "unknown",
        "health": "none",
        "cpu_percent": 0.0,
        "cpu_percent_raw": 0.0,
        "cpu_count": 1,
        "mem_usage": 0,
        "mem_limit": 0,
        "mem_percent": 0.0,
        "mem_cache": 0,
        "network_rx": 0,
        "network_tx": 0,
        "disk_usage": None,
        "disk_limit": None,
        "disk_percent": None,
    }


def _stats_payload(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Map an agent stats result to the façade Homy contract.

    - ``state``/``health`` are normalised;
    - ``cpu_percent`` is normalised to host 0–100 (``cpu_percent_raw`` and
      ``cpu_count`` keep the agent value / vCPU count visible);
    - memory fields come from the agent (already ``usage - cache`` based) and
      ``mem_cache`` exposes the deducted page cache;
    - ``disk_*`` are ``null`` (unavailable via ``docker stats``).
    """
    raw_cpu = _as_float(raw.get("cpu_percent"), 0.0)
    cpu_count = _as_int(raw.get("cpu_count"), 1)
    if cpu_count <= 0:
        cpu_count_reported = 1
    else:
        cpu_count_reported = cpu_count
    return {
        "state": normalize_state(raw.get("state")),
        "health": normalize_health(raw.get("health")),
        "cpu_percent": normalize_cpu_percent(raw_cpu, cpu_count),
        "cpu_percent_raw": round(raw_cpu, 2),
        "cpu_count": cpu_count_reported,
        "mem_usage": max(0, _as_int(raw.get("mem_usage"), 0)),
        "mem_limit": max(0, _as_int(raw.get("mem_limit"), 0)),
        "mem_percent": round(_as_float(raw.get("mem_percent"), 0.0), 2),
        "mem_cache": max(0, _as_int(raw.get("mem_cache"), 0)),
        "network_rx": max(0, _as_int(raw.get("network_rx"), 0)),
        "network_tx": max(0, _as_int(raw.get("network_tx"), 0)),
        "disk_usage": None,
        "disk_limit": None,
        "disk_percent": None,
    }


def _stats_error(agent: str, container: str, code: str, message: str) -> Dict[str, Any]:
    """Build a per-target batch stats error result (uniform shape, zeros)."""
    return {
        "agent": agent,
        "container": container,
        "found": False,
        **_zero_stats_payload(),
        "error": {"code": code, "message": message},
    }


def _stats_found(agent: str, container: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    """Build a per-target batch stats success result (``error: null``)."""
    return {
        "agent": agent,
        "container": container,
        "found": True,
        **_stats_payload(raw),
        "error": None,
    }


def _sample_to_stats_raw(sample: Dict[str, Any]) -> Dict[str, Any]:
    """Map a streamed sample to the ``_stats_payload`` input shape.

    The streamed payload trims ``network_rx``/``network_tx`` to
    ``net_rx``/``net_tx`` and never carries ``health``; both differences are
    normalised here so a hot-cache hit produces **exactly** the same response
    as a live ``docker stats`` snapshot.
    """
    return {
        "state": sample.get("state"),
        "health": sample.get("health"),
        "cpu_percent": sample.get("cpu_percent"),
        "cpu_count": sample.get("cpu_count"),
        "mem_usage": sample.get("mem_usage"),
        "mem_limit": sample.get("mem_limit"),
        "mem_percent": sample.get("mem_percent"),
        "mem_cache": sample.get("mem_cache"),
        "network_rx": sample.get("net_rx"),
        "network_tx": sample.get("net_tx"),
    }


def _checked_at_from_ms(ts: Any) -> str:
    """Format an epoch-ms timestamp as the façade ISO-8601 ``...Z`` checkedAt."""
    try:
        return datetime.fromtimestamp(float(ts) / 1000.0, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return utc_iso_now()


def _stats_found_from_sample(
    agent: str, container: str, sample: Dict[str, Any]
) -> Dict[str, Any]:
    """Build a batch stats success result from a fresh hot-set sample."""
    return _stats_found(agent, container, _sample_to_stats_raw(sample))


def _agent_gate(agent: str):
    """Resolve an agent for the façade, returning ``(info, error_response)``.

    ``error_response`` is ``None`` when the agent is known and online/unknown;
    otherwise it is a ready-to-return ``404`` (unknown) / ``503`` (offline)
    response, keeping the ``{error, code}`` envelope.
    """
    manager = _manager()
    if agent not in manager.agents:
        return None, _error(404, f"Agent '{agent}' not found", "agent_not_found")
    info = manager.agents[agent]
    if info.get("status") == "offline":
        return None, _error(503, f"Agent '{agent}' is offline", "agent_offline")
    return info, None


def _first_agent_result(data: Any) -> Optional[Dict[str, Any]]:
    """Extract ``results[0]`` from an agent batch-stats payload (or None)."""
    if not isinstance(data, dict):
        return None
    results = data.get("results")
    if not isinstance(results, list) or not results:
        return None
    first = results[0]
    return first if isinstance(first, dict) else None


# ---------------------------------------------------------------------------
# Endpoints (LOT A)
# ---------------------------------------------------------------------------

@router.get("/agents", dependencies=[Depends(_check_integration_auth)])
async def list_agents():
    """List every configured agent with its status, version and last check."""
    manager = _manager()
    if not manager.agents:
        return _error(503, "No agents configured", "no_agents")
    agents = [
        {
            "name": name,
            "url": info.get("url", ""),
            "status": info.get("status", "unknown"),
            "version": info.get("version") or None,
            "lastCheck": iso_from_timestamp(info.get("last_check")),
        }
        for name, info in manager.agents.items()
    ]
    return {"agents": agents}


@router.get("/agents/{agent}/containers", dependencies=[Depends(_check_integration_auth)])
async def list_agent_containers(agent: str):
    """List the normalised containers of one agent."""
    manager = _manager()
    if agent not in manager.agents:
        return _error(404, f"Agent '{agent}' not found", "agent_not_found")
    info = manager.agents[agent]
    if info.get("status") == "offline":
        return _error(502, f"Agent '{agent}' is unreachable", "agent_unreachable")
    try:
        containers = await manager.get_containers(agent)
    except Exception as exc:  # defensive: client methods normally swallow errors
        logger.warning("integration list_agent_containers failed for '%s': %s", agent, exc)
        return _error(502, f"Agent '{agent}' is unreachable", "agent_unreachable")
    if not containers and info.get("status") != "online":
        return _error(502, f"Agent '{agent}' is unreachable", "agent_unreachable")
    normalized = [normalize_container(c) for c in containers if isinstance(c, dict)]
    return {"agent": agent, "containers": normalized}


@router.get("/agents/{agent}/containers/{container}", dependencies=[Depends(_check_integration_auth)])
async def get_agent_container(agent: str, container: str):
    """Return one container (matched by name OR id) with its current state."""
    manager = _manager()
    if agent not in manager.agents:
        return _error(404, f"Agent '{agent}' not found", "agent_not_found")
    info = manager.agents[agent]
    status = info.get("status")
    if status == "offline":
        return _error(503, f"Agent '{agent}' is offline", "agent_offline")
    try:
        data = await manager.get_container(agent, container)
    except Exception as exc:
        logger.warning("integration get_agent_container failed for '%s/%s': %s", agent, container, exc)
        return _error(502, f"Agent '{agent}' is unreachable", "agent_unreachable")
    if data is None:
        if status != "online":
            return _error(502, f"Agent '{agent}' is unreachable", "agent_unreachable")
        return _error(404, f"Container '{container}' not found on agent '{agent}'", "not_found")
    normalized = normalize_container(data)
    return {
        "agent": agent,
        "container": container,
        "id": normalized["id"],
        "name": normalized["name"],
        "state": normalized["state"],
        "health": normalized["health"],
        "checkedAt": utc_iso_now(),
    }


@router.post("/containers/health", dependencies=[Depends(_check_integration_auth)])
async def batch_container_health(request: Request):
    """Batch health check for up to :data:`MAX_BATCH_TARGETS` ``{agent, container}``.

    Always answers ``200 {checkedAt, results:[...]}`` (order preserved): every
    per-target failure — unknown container, offline/unreachable agent, unknown
    agent — is reported inside its own result slot, never as a global error.
    Only malformed requests (invalid JSON, ``targets`` not a list, too many
    targets, body too large) yield a ``400 {error, code:"invalid_request"}``.
    """
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return _error(400, "Request body too large (max 256 KiB)", "invalid_request")
    try:
        payload = json.loads(raw) if raw else {}
    except (ValueError, UnicodeDecodeError):
        return _error(400, "Invalid JSON body", "invalid_request")
    if not isinstance(payload, dict):
        return _error(400, "Request body must be a JSON object", "invalid_request")
    targets = payload.get("targets")
    if not isinstance(targets, list):
        return _error(400, "Field 'targets' must be a list", "invalid_request")
    if len(targets) > MAX_BATCH_TARGETS:
        return _error(
            400,
            f"Too many targets (max {MAX_BATCH_TARGETS})",
            "invalid_request",
        )

    manager = _manager()
    checked_at = utc_iso_now()

    # Group target indices by agent so each agent's container list is fetched
    # exactly once (cache-SWR accepted, ~5s freshness via get_containers).
    by_agent: Dict[str, List[int]] = {}
    for index, target in enumerate(targets):
        agent = target.get("agent") if isinstance(target, dict) else None
        agent = agent if isinstance(agent, str) else ""
        by_agent.setdefault(agent, []).append(index)

    known_agents = [
        agent
        for agent in by_agent
        if agent
        and agent in manager.agents
        and manager.agents[agent].get("status") != "offline"
    ]
    fetched: Dict[str, Any] = {}
    if known_agents:
        results = await asyncio.gather(
            *(manager.get_containers(agent) for agent in known_agents),
            return_exceptions=True,
        )
        fetched = dict(zip(known_agents, results))

    results: List[Optional[Dict[str, Any]]] = [None] * len(targets)
    for agent, indices in by_agent.items():
        for index in indices:
            target = targets[index]
            container = target.get("container") if isinstance(target, dict) else None
            container = container if isinstance(container, str) else ""
            if not isinstance(target, dict):
                results[index] = _result_error(
                    agent, container, "invalid_request", "Target must be a JSON object"
                )
                continue
            if not agent or agent not in manager.agents:
                results[index] = _result_error(
                    agent, container, "agent_unreachable", f"Agent '{agent}' not found"
                )
                continue
            if manager.agents[agent].get("status") == "offline":
                results[index] = _result_error(
                    agent, container, "agent_unreachable", f"Agent '{agent}' is offline"
                )
                continue
            listed = fetched.get(agent)
            if isinstance(listed, Exception) or not isinstance(listed, list):
                results[index] = _result_error(
                    agent, container, "agent_unreachable", f"Agent '{agent}' is unreachable"
                )
                continue
            if not listed and manager.agents[agent].get("status") != "online":
                results[index] = _result_error(
                    agent, container, "agent_unreachable", f"Agent '{agent}' is unreachable"
                )
                continue
            match = _match_container(listed, container)
            if match is None:
                results[index] = _result_error(
                    agent, container, "not_found", f"Container '{container}' not found"
                )
                continue
            normalized = normalize_container(match)
            results[index] = {
                "agent": agent,
                "container": container,
                "found": True,
                "id": normalized["id"],
                "name": normalized["name"],
                "state": normalized["state"],
                "health": normalized["health"],
            }

    return {"checkedAt": checked_at, "results": results}


# ---------------------------------------------------------------------------
# Endpoints (LOT B) — stats
# ---------------------------------------------------------------------------

@router.get("/agents/{agent}/containers/{container}/stats", dependencies=[Depends(_check_integration_auth)])
async def get_container_stats(agent: str, container: str):
    """Return one container's live stats (matched by name OR id).

    Shape: ``{agent, container, state, health, cpu_percent, cpu_count,
    cpu_percent_raw, mem_usage, mem_limit, mem_percent, mem_cache,
    network_rx, network_tx, disk_usage:null, disk_limit:null,
    disk_percent:null, checkedAt}``.

    A **stopped** container answers ``200`` with counters at ``0`` and its
    real ``state``/``health`` (not an error). Error mapping: unknown agent →
    ``404 agent_not_found``; unknown container → ``404 not_found``; offline
    agent → ``503 agent_offline``; unreachable agent → ``502
    agent_unreachable``; agent timeout → ``504 timeout``.
    """
    _info, err = _agent_gate(agent)
    if err is not None:
        return err
    manager = _manager()

    # Register the request in the Homy hot set (TTL refreshed) and serve from
    # the streamed cache when a fresh (< 2 s) sample exists. The response
    # contract is unchanged: a cache hit just makes ``checkedAt`` the sample's
    # timestamp instead of the request time.
    stats_manager = _stats_manager()
    with contextlib.suppress(Exception):
        await stats_manager.mark_hot_set(agent, [container])
    with contextlib.suppress(Exception):
        sample = stats_manager.get_fresh_sample(agent, container, HOT_SAMPLE_MAX_AGE)
        if sample is not None:
            return {
                "agent": agent,
                "container": container,
                "checkedAt": _checked_at_from_ms(sample.get("ts")),
                **_stats_payload(_sample_to_stats_raw(sample)),
            }

    try:
        data = await manager.fetch_containers_stats(agent, [container])
    except httpx.TimeoutException:
        return _error(504, f"Stats for '{container}' timed out", "timeout")
    except Exception as exc:
        logger.warning(
            "integration get_container_stats failed for '%s/%s': %s", agent, container, exc
        )
        return _error(502, f"Agent '{agent}' is unreachable", "agent_unreachable")
    result = _first_agent_result(data)
    if result is None:
        return _error(502, f"Agent '{agent}' is unreachable", "agent_unreachable")
    if not result.get("found", False):
        return _error(
            404,
            f"Container '{container}' not found on agent '{agent}'",
            "not_found",
        )
    payload = _stats_payload(result)
    return {
        "agent": agent,
        "container": container,
        "checkedAt": utc_iso_now(),
        **payload,
    }


@router.get(
    "/agents/{agent}/containers/{container}/stats/history",
    dependencies=[Depends(_check_integration_auth)],
)
async def get_container_stats_history(agent: str, container: str, window: int = 900):
    """Return one container's merged stats history over a supported window.

    Proxies the agent's ``GET /agent/containers/{id}/stats/history`` (merged
    fine ring buffer + downsampled SQLite points). ``window`` ∈ ``900 | 3600 |
    86400`` seconds. Payload is passed through unchanged:
    ``{container, window, points:[{ts, cpu_percent, mem_usage, mem_limit,
    mem_percent, net_rx, net_tx}, ...]}``.

    Error envelope is the façade ``{error, code}``: unsupported ``window`` →
    ``400 invalid_request``; unknown agent → ``404 agent_not_found``; offline
    agent → ``503 agent_offline``; unknown container → ``404 not_found``;
    unreachable agent → ``502 agent_unreachable``; timeout → ``504 timeout``.
    """
    if window not in SUPPORTED_STATS_WINDOWS:
        return _error(400, f"Unsupported window: {window}", "invalid_request")
    _info, err = _agent_gate(agent)
    if err is not None:
        return err
    manager = _manager()
    try:
        data = await manager._request(
            agent,
            "GET",
            f"/agent/containers/{container}/stats/history",
            params={"window": window},
            timeout=STATS_TIMEOUT,
        )
    except httpx.TimeoutException:
        return _error(504, f"History for '{container}' timed out", "timeout")
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        if status_code == 404:
            return _error(
                404,
                f"Container '{container}' not found on agent '{agent}'",
                "not_found",
            )
        if status_code == 400:
            return _error(400, f"Unsupported window: {window}", "invalid_request")
        logger.warning(
            "integration history failed for '%s/%s': HTTP %s",
            agent,
            container,
            status_code,
        )
        return _error(502, f"Agent '{agent}' is unreachable", "agent_unreachable")
    except Exception as exc:
        logger.warning(
            "integration history failed for '%s/%s': %s", agent, container, exc
        )
        return _error(502, f"Agent '{agent}' is unreachable", "agent_unreachable")
    return data


@router.post("/containers/stats", dependencies=[Depends(_check_integration_auth)])
async def batch_container_stats(request: Request):
    """Batch stats for up to :data:`MAX_BATCH_TARGETS` ``{agent, container}``.

    Always answers ``200 {checkedAt, results:[...]}`` (input order preserved):
    per-target failures (unknown container/agent, offline/unreachable agent,
    agent timeout) are reported inside their own slot with an ``error``
    object. Only malformed requests (invalid JSON, ``targets`` not a list,
    too many targets, body > 256 KiB) yield ``400 invalid_request``.

    Efficiency: targets are grouped by agent and each agent is queried with
    **one** HTTP call to ``POST /agent/containers/stats``. Stats are fetched
    live (never cached) for real freshness; that means one ``docker stats``
    round-trip per requested container on the agent side (bounded fan-out).
    """
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return _error(400, "Request body too large (max 256 KiB)", "invalid_request")
    try:
        payload = json.loads(raw) if raw else {}
    except (ValueError, UnicodeDecodeError):
        return _error(400, "Invalid JSON body", "invalid_request")
    if not isinstance(payload, dict):
        return _error(400, "Request body must be a JSON object", "invalid_request")
    targets = payload.get("targets")
    if not isinstance(targets, list):
        return _error(400, "Field 'targets' must be a list", "invalid_request")
    if len(targets) > MAX_BATCH_TARGETS:
        return _error(400, f"Too many targets (max {MAX_BATCH_TARGETS})", "invalid_request")

    manager = _manager()
    checked_at = utc_iso_now()

    by_agent: Dict[str, List[int]] = {}
    for index, target in enumerate(targets):
        agent = target.get("agent") if isinstance(target, dict) else None
        agent = agent if isinstance(agent, str) else ""
        by_agent.setdefault(agent, []).append(index)

    results: List[Optional[Dict[str, Any]]] = [None] * len(targets)
    for agent, indices in by_agent.items():
        refs: List[str] = []
        for index in indices:
            target = targets[index]
            ref = target.get("container") if isinstance(target, dict) else None
            refs.append(ref if isinstance(ref, str) else "")

        valid: List[int] = []
        for index, ref in zip(indices, refs):
            if not isinstance(targets[index], dict):
                results[index] = _stats_error(
                    agent, ref, "invalid_request", "Target must be a JSON object"
                )
            elif not agent or agent not in manager.agents:
                results[index] = _stats_error(
                    agent, ref, "agent_unreachable", f"Agent '{agent}' not found"
                )
            elif manager.agents[agent].get("status") == "offline":
                results[index] = _stats_error(
                    agent, ref, "agent_unreachable", f"Agent '{agent}' is offline"
                )
            else:
                valid.append(index)
        if not valid:
            continue

        # Hot-set: register every requested (valid) container for the Homy TTL
        # and serve fresh samples (< 2 s) straight from the streamed cache.
        # Only the remaining refs pay a live ``docker stats`` round-trip.
        stats_manager = _stats_manager()
        with contextlib.suppress(Exception):
            await stats_manager.mark_hot_set(agent, [refs[index] for index in valid])
        fetch_valid: List[int] = []
        for index in valid:
            sample = None
            with contextlib.suppress(Exception):
                sample = stats_manager.get_fresh_sample(
                    agent, refs[index], HOT_SAMPLE_MAX_AGE
                )
            if sample is not None:
                results[index] = _stats_found_from_sample(agent, refs[index], sample)
            else:
                fetch_valid.append(index)
        if not fetch_valid:
            continue
        valid = fetch_valid

        valid_refs = [refs[index] for index in valid]
        try:
            data = await manager.fetch_containers_stats(agent, valid_refs)
        except httpx.TimeoutException:
            for index in valid:
                results[index] = _stats_error(
                    agent, refs[index], "timeout", f"Stats for agent '{agent}' timed out"
                )
            continue
        except Exception as exc:
            logger.warning("integration batch stats failed for '%s': %s", agent, exc)
            for index in valid:
                results[index] = _stats_error(
                    agent, refs[index], "agent_unreachable", f"Agent '{agent}' is unreachable"
                )
            continue

        agent_results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(agent_results, list):
            for index in valid:
                results[index] = _stats_error(
                    agent, refs[index], "agent_unreachable", f"Agent '{agent}' is unreachable"
                )
            continue
        for position, index in enumerate(valid):
            candidate = (
                agent_results[position]
                if position < len(agent_results)
                and isinstance(agent_results[position], dict)
                else None
            )
            if candidate is None or not candidate.get("found", False):
                results[index] = _stats_error(
                    agent,
                    refs[index],
                    "not_found",
                    f"Container '{refs[index]}' not found on agent '{agent}'",
                )
            else:
                results[index] = _stats_found(agent, refs[index], candidate)

    return {"checkedAt": checked_at, "results": results}


# ---------------------------------------------------------------------------
# Endpoints (LOT B) — actions
# ---------------------------------------------------------------------------

@router.post(
    "/agents/{agent}/containers/{container}/{action}",
    dependencies=[Depends(_check_integration_auth)],
)
async def container_action(agent: str, container: str, action: str):
    """Run ``start`` / ``stop`` / ``restart`` on one container.

    Answers ``200 {success:true, agent, container, action, state, health}``
    where ``state``/``health`` are **re-read after** the action (a ``restart``
    may briefly report ``running`` + ``starting``/``none``).

    Codes: ``404 not_found`` (unknown agent/container/action); ``409 conflict
    {state}`` when the container is already in the target state
    (idempotence: ``start`` on running / ``stop`` on stopped); ``502
    agent_unreachable`` (transport error or agent-side action failure); ``503
    not_configured``/``agent_offline``; ``504 timeout`` when the action
    exceeds :data:`ACTION_TIMEOUT`.
    """
    if action not in ("start", "stop", "restart"):
        return _error(404, f"Unknown action '{action}'", "not_found")
    _info, err = _agent_gate(agent)
    if err is not None:
        return err
    manager = _manager()

    # Pre-check: resolve the current state so we can 404 (unknown container)
    # and 409 (already in the target state) before mutating anything.
    try:
        before = await manager.fetch_containers_stats(agent, [container])
    except httpx.TimeoutException:
        return _error(504, "Action timed out", "timeout")
    except Exception as exc:
        logger.warning("integration action pre-check failed for '%s/%s': %s", agent, container, exc)
        return _error(502, f"Agent '{agent}' is unreachable", "agent_unreachable")
    current = _first_agent_result(before)
    if current is None:
        return _error(502, f"Agent '{agent}' is unreachable", "agent_unreachable")
    if not current.get("found", False):
        return _error(
            404,
            f"Container '{container}' not found on agent '{agent}'",
            "not_found",
        )
    current_state = normalize_state(current.get("state"))
    if action == "start" and current_state == "running":
        return JSONResponse(
            status_code=409,
            content={"error": "Container is already running", "code": "conflict", "state": current_state},
        )
    if action == "stop" and current_state in STOPPED_STATES:
        return JSONResponse(
            status_code=409,
            content={"error": "Container is already stopped", "code": "conflict", "state": current_state},
        )

    try:
        agent_result = await manager.action_container(agent, container, action)
    except httpx.TimeoutException:
        return _error(504, "Action timed out", "timeout")
    except Exception as exc:
        logger.warning("integration action '%s' failed for '%s/%s': %s", action, agent, container, exc)
        return _error(502, f"Agent '{agent}' is unreachable", "agent_unreachable")
    if isinstance(agent_result, dict) and agent_result.get("success") is False:
        return _error(
            502,
            f"Action '{action}' failed on agent '{agent}'",
            "action_failed",
        )

    # Best-effort re-read of the resulting state; the action itself succeeded,
    # so a failed re-read falls back to the action's expected state.
    final_state = "running" if action in ("start", "restart") else "exited"
    final_health = "none"
    try:
        after = await manager.fetch_containers_stats(agent, [container])
        refreshed = _first_agent_result(after)
        if refreshed is not None and refreshed.get("found", False):
            final_state = normalize_state(refreshed.get("state"))
            final_health = normalize_health(refreshed.get("health"))
    except Exception:
        pass
    return {
        "success": True,
        "agent": agent,
        "container": container,
        "action": action,
        "state": final_state,
        "health": final_health,
    }
