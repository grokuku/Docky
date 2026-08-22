"""Cache management for agents (per-agent + aggregate stale-while-revalidate).

Extracted from ``app.agent_manager.client``. Every function is written as a
method (``self`` first) and assigned back onto ``AgentManager`` in the façade,
so behaviour is unchanged.

The stale-while-revalidate clock reads ``time.time`` through the façade
namespace at call time (``_time``). The tests monkeypatch
``app.agent_manager.client.time.time``; resolving through the façade keeps
that patch effective for the code living in this sub-module.
"""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Per-agent cache entries older than this many seconds are refreshed from the
# network when rebuilding the aggregate cache.
_AGENT_CACHE_TTL = 60.0


def _time() -> float:
    """Return the current time, read from the façade namespace.

    ``app.agent_manager.client.time.time`` is the clock the test suite
    monkeypatches; late resolution avoids an import cycle and keeps the patch
    effective for this sub-module.
    """
    from app.agent_manager import client

    return client.time.time()


def _load_cache(self):
    """Load cache from disk if available."""
    try:
        if os.path.exists(self._cache_path):
            with open(self._cache_path) as f:
                saved = json.load(f)
                if isinstance(saved, dict):
                    self._cache = saved
    except Exception:
        logger.warning("Failed to load cache from %s; resetting to defaults", self._cache_path)
        self._cache = {
            "containers": {"data": None, "timestamp": 0, "pending": False},
            "stacks": {"data": None, "timestamp": 0, "pending": False},
            "ports": {"data": None, "timestamp": 0, "pending": False},
        }


def _save_cache(self):
    """Persist cache to disk."""
    try:
        os.makedirs(os.path.dirname(self._cache_path), exist_ok=True)
        with open(self._cache_path, "w") as f:
            json.dump(self._cache, f, default=str)
    except Exception:
        logger.exception("Failed to persist cache to %s", self._cache_path)


async def refresh_cache(self, agent_name: str):
    """Fetch containers, stacks and ports for an agent and cache them."""
    containers, stacks, ports = await asyncio.gather(
        self.get_containers(agent_name),
        self.get_stacks(agent_name),
        self.get_ports(agent_name),
        return_exceptions=True,
    )
    self.cache[agent_name] = {
        "containers": containers if isinstance(containers, list) else [],
        "stacks": stacks if isinstance(stacks, list) else [],
        "ports": ports if isinstance(ports, list) else [],
        "timestamp": _time(),
    }


async def invalidate_cache(self, agent_name: Optional[str] = None):
    """Force invalidation of the cache after an action on an agent.

    Removes the per-agent cache entry (or all of them when *agent_name* is
    ``None``), invalidates the aggregate stale-while-revalidate cache and
    rebuilds it immediately. A subsequent ``refreshStacks()`` / ``get_cached_*``
    call therefore returns the new state instead of stale-while-revalidate
    data.
    """
    if agent_name is not None:
        self.cache.pop(agent_name, None)
    else:
        self.cache.clear()
    # Invalidate the aggregate cache so the next read triggers a rebuild.
    for entry in self._cache.values():
        entry["data"] = None
        entry["timestamp"] = 0
        entry["pending"] = False
    try:
        await self._rebuild_aggregate_cache()
    except Exception as e:
        logger.warning("Cache rebuild after invalidation failed: %s", e)


def _get_cached_containers(self, agent_name: str) -> List[Dict[str, Any]]:
    """Return cached containers for an agent, or an empty list."""
    return self.cache.get(agent_name, {}).get("containers", [])


def _get_cached_stacks(self, agent_name: str) -> List[Dict[str, Any]]:
    """Return cached stacks for an agent, or an empty list."""
    return self.cache.get(agent_name, {}).get("stacks", [])


def _get_cached_ports(self, agent_name: str) -> List[Dict[str, Any]]:
    """Return cached ports for an agent, or an empty list."""
    return self.cache.get(agent_name, {}).get("ports", [])


def _get_cached_or_refresh(self, key: str, fetch_func) -> Optional[List[Dict[str, Any]]]:
    """Stale-while-revalidate: retourne le cache immédiatement, refresh en arrière-plan.

    Returns cached data (even if stale) or None if no cache exists yet.
    When stale data is returned, a background refresh is triggered.
    """
    cache = self._cache[key]
    now = _time()

    # Cache frais (< 5s) → retour immédiat
    if cache["data"] is not None and now - cache["timestamp"] < 5:
        return cache["data"]

    # Cache périmé mais existant → retourne le cache + refresh en arrière-plan
    if cache["data"] is not None and not cache["pending"]:
        cache["pending"] = True
        loop = asyncio.get_event_loop()
        if loop and loop.is_running():
            asyncio.ensure_future(self._refresh_cache_entry(key, fetch_func))
        return cache["data"]

    # Pas de cache du tout → retourne None (le caller fera un fetch direct)
    return None


async def _refresh_cache_entry(self, key: str, fetch_func):
    """Rafraîchit une entrée du cache en arrière-plan."""
    try:
        data = await fetch_func()
        self._cache[key]["data"] = data
        self._cache[key]["timestamp"] = _time()
    except Exception as e:
        logger.error("Cache refresh failed for %s: %s", key, e)
    finally:
        self._cache[key]["pending"] = False


async def _rebuild_aggregate_cache(self):
    """Rebuild aggregate cache from per-agent caches when possible.

    Falling back to the network only for agents whose cache is missing or
    stale avoids an extra N+1 request burst after every incremental refresh.
    """
    all_containers = []
    all_stacks = []
    all_ports = []

    for name in self.agents:
        cached = self.cache.get(name)
        if cached and (_time() - cached.get("timestamp", 0)) < _AGENT_CACHE_TTL:
            c = cached.get("containers", [])
            s = cached.get("stacks", [])
            p = cached.get("ports", [])
        else:
            c, s, p = await asyncio.gather(
                self.get_containers(name),
                self.get_stacks(name),
                self.get_ports(name),
                return_exceptions=True,
            )
            c = c if isinstance(c, list) else []
            s = s if isinstance(s, list) else []
            p = p if isinstance(p, list) else []

        for container in c:
            if isinstance(container, dict):
                container["agent_name"] = name
                all_containers.append(container)

        for stack in s:
            if isinstance(stack, dict):
                stack["agent_name"] = name
                all_stacks.append(stack)

        for port in p:
            if isinstance(port, dict):
                port["agent_name"] = name
                all_ports.append(port)

    self._cache["containers"]["data"] = all_containers
    self._cache["containers"]["timestamp"] = _time()
    self._cache["stacks"]["data"] = all_stacks
    self._cache["stacks"]["timestamp"] = _time()
    self._cache["ports"]["data"] = all_ports
    self._cache["ports"]["timestamp"] = _time()
    self._save_cache()


async def ensure_cache(self, key: str) -> Optional[List[Dict[str, Any]]]:
    """Attend que le cache soit rempli s'il est vide (premier appel)."""
    cache = self._cache[key]
    if cache["data"] is not None:
        return cache["data"]

    if not cache["pending"]:
        if key == "containers":
            coro = self.get_all_containers()
        elif key == "stacks":
            coro = self.get_all_stacks()
        elif key == "ports":
            coro = self.get_all_ports()
        else:
            return None

        cache["pending"] = True
        try:
            data = await coro
            cache["data"] = data
            cache["timestamp"] = _time()
            return data
        finally:
            cache["pending"] = False

    # Si déjà en refresh mais pas de données, le caller attendra un retry
    return None


async def get_cached_containers(self) -> Optional[List[Dict[str, Any]]]:
    """Retourne les containers de tous les agents (cachés si possible).

    Utilise le stale-while-revalidate: retourne les données périmées
    immédiatement et rafraîchit en arrière-plan.
    """
    data = self._get_cached_or_refresh("containers", self.get_all_containers)
    if data is not None:
        return data
    # Cache vide (premier appel) → fetch direct et attend
    return await self.ensure_cache("containers")


async def get_cached_stacks(self) -> Optional[List[Dict[str, Any]]]:
    """Retourne les stacks de tous les agents (cachées si possible)."""
    data = self._get_cached_or_refresh("stacks", self.get_all_stacks)
    if data is not None:
        return data
    return await self.ensure_cache("stacks")


async def get_cached_ports(self) -> Optional[List[Dict[str, Any]]]:
    """Retourne les ports de tous les agents (cachés si possible)."""
    data = self._get_cached_or_refresh("ports", self.get_all_ports)
    if data is not None:
        return data
    return await self.ensure_cache("ports")


async def refresh_all_caches(self):
    """Refresh per-agent caches and populate the aggregate cache.

    Called periodically by ``start_background_refresh`` or on demand.
    Each category is fetched independently so a single failure does not
    block the others.
    """
    # 1) Refresh per-agent caches (existing behaviour)
    names = [
        name
        for name, info in self.agents.items()
        if info["status"] in ("online", "unknown")
    ]
    tasks = [self.refresh_cache(name) for name in names]
    if tasks:
        await asyncio.gather(*tasks)

    # 2) Populate the aggregate cache
    try:
        containers = await self.get_all_containers()
        self._cache["containers"]["data"] = containers
        self._cache["containers"]["timestamp"] = _time()
    except Exception as e:
        logger.warning("refresh_all_caches containers failed: %s", e)

    try:
        stacks = await self.get_all_stacks()
        self._cache["stacks"]["data"] = stacks
        self._cache["stacks"]["timestamp"] = _time()
    except Exception as e:
        logger.warning("refresh_all_caches stacks failed: %s", e)

    try:
        ports = await self.get_all_ports()
        self._cache["ports"]["data"] = ports
        self._cache["ports"]["timestamp"] = _time()
    except Exception as e:
        logger.warning("refresh_all_caches ports failed: %s", e)

    # 3) Persist cache to disk
    self._save_cache()
