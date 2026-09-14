"""Real-time container stats streaming for the Docky orchestrator (LOT 2).

This module is the orchestrator-side counterpart of the agent's
``agent/stats_stream.py`` (LOT 1). It owns:

- **one WS connection per agent** to ``/agent/stats/stream`` (opened on demand
  as soon as one of its containers is watched, reconnected with exponential
  backoff, closed when nothing is watched anymore);
- a **unified hot set** with per-container refcounts and sources: ``ui``
  (browser tiles, lives until the last frontend unsubscribes) and ``homy``
  (integration façade calls, kept alive by a 60 s TTL refreshed on every REST
  call);
- a **periodic re-watch** of the watched refs (~20 s, below the agent's 30 s
  watch TTL) plus an explicit ``unwatch`` when a container leaves the hot set;
- a **dispatch** of every live sample to (a) a per-container last-sample cache
  used by the integration façade, (b) the frontend subscribers of
  ``WS /api/stats/stream``, and the retention of the last snapshot for new
  subscribers.

Why sources + refcounts
-----------------------

The same container can be watched for two independent reasons (a browser tile
**and** a Homy REST poll). A boolean watch flag would break as soon as one of
the two consumers goes away. Each source therefore has its own count, and the
container is only released when every source reaches zero (or, for ``homy``,
when its TTL expires). ``touch``/``mark_hot_set`` refresh an existing presence
without incrementing the count, so a busy Homy poller cannot leak watches.

Everything runs on the application event loop; the manager is intentionally
free of threads (unlike the agent's per-container streamer). The only injected
seams are the ``AgentManager``-like provider (metadata + TLS + HTTP) and
``websockets.connect`` so tests can drive it without any real network.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

#: Source label used by browser clients (no TTL, released on unsubscribe).
SOURCE_UI = "ui"
#: Source label used by the integration façade (60 s TTL, refreshed on call).
SOURCE_HOMY = "homy"

#: How long a Homy entry stays in the hot set without being refreshed.
DEFAULT_HOMY_TTL = 60.0
#: Re-watch cadence (must stay below the agent's 30 s watch TTL).
DEFAULT_REWATCH_INTERVAL = 20.0
#: Hot-set expiry sweep cadence.
DEFAULT_SWEEP_INTERVAL = 5.0
#: Bounded frontend queue size (slow clients drop the oldest sample).
DEFAULT_QUEUE_SIZE = 200
#: A cached sample younger than this is served by the integration façade.
DEFAULT_FRESH_SECONDS = 2.0
#: Reconnection backoff bounds (seconds).
DEFAULT_BACKOFF_MIN = 1.0
DEFAULT_BACKOFF_MAX = 30.0
#: Timeout for the REST watch/unwatch calls toward an agent.
WATCH_HTTP_TIMEOUT = 10.0

#: Supported history windows (seconds) — mirrors the agent contract.
SUPPORTED_WINDOWS: Tuple[int, ...] = (900, 3600, 86400)


def _default_manager_provider():
    """Late-resolve the process-wide ``AgentManager`` singleton.

    Resolved lazily so importing this module never triggers the
    ``app.agent_manager.client`` import chain (no cycle) and so tests can swap
    the singleton in ``app.routes.api``.
    """
    from app.agent_manager.client import agent_manager

    return agent_manager


def _ws_url(base_url: str) -> str:
    """Convert an agent HTTP(S) base URL to its WS(S) equivalent."""
    base = (base_url or "").rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):]
    if base.startswith("http://"):
        return "ws://" + base[len("http://"):]
    return base


def refs_match(ref: str, container_id: str, name: str) -> bool:
    """Docker-ish match between a user ref and a sample ``(id, name)``.

    Accepts exact id, exact name, or an unambiguous id prefix (≥ 6 chars, the
    same threshold Docker uses) in either direction (a full 64-char id starts
    with the 12-char ``short_id`` exposed by the agent).
    """
    if not ref:
        return False
    if ref == container_id or ref == name:
        return True
    if len(ref) >= 6 and container_id.startswith(ref):
        return True
    if container_id and len(container_id) >= 6 and ref.startswith(container_id):
        return True
    return False


@dataclass
class _Subscriber:
    """A frontend WS client plus the set of ``(agent, container)`` it watches."""

    loop: Any
    queue: "asyncio.Queue"
    targets: Set[Tuple[str, str]] = field(default_factory=set)


@dataclass
class _Watch:
    """One hot-set entry: per-source refcounts + the last sample seen."""

    container: str
    counts: Dict[str, int] = field(default_factory=dict)
    expires: Dict[str, float] = field(default_factory=dict)
    last_sample: Optional[Dict[str, Any]] = None

    def active(self) -> bool:
        return any(count > 0 for count in self.counts.values())


@dataclass
class _AgentState:
    """Streaming state for one agent."""

    name: str
    watches: Dict[str, _Watch] = field(default_factory=dict)
    #: last sample per canonical container id (hot cache for the façade).
    last_samples: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: container name -> canonical id (for name-based hot lookups).
    name_index: Dict[str, str] = field(default_factory=dict)
    #: refs currently sent to the agent via ``/agent/stats/watch``.
    sent_refs: Set[str] = field(default_factory=set)
    task: Optional[asyncio.Task] = None
    connected: bool = False


class AgentStatsStreamManager:
    """Fan-in (one WS per agent) / fan-out (N frontend WS) stats hub.

    Public surface (all calls happen on the application event loop):

    - ``subscribe(agent, container, source)`` / ``unsubscribe(...)`` — refcount
      the hot set;
    - ``touch(agent, containers, source)`` / ``mark_hot_set(...)`` — mark a
      presence and refresh its TTL (idempotent, no count leak);
    - ``add_subscriber`` / ``remove_subscriber`` / ``client_subscribe`` /
      ``client_unsubscribe`` — frontend WS plumbing;
    - ``get_fresh_sample`` / ``get_samples_for_targets`` — read the hot cache;
    - ``sweep`` / ``start`` / ``stop`` — lifecycle.
    """

    def __init__(
        self,
        manager: Any = None,
        *,
        manager_provider: Optional[Callable[[], Any]] = None,
        homy_ttl: float = DEFAULT_HOMY_TTL,
        rewatch_interval: float = DEFAULT_REWATCH_INTERVAL,
        sweep_interval: float = DEFAULT_SWEEP_INTERVAL,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        fresh_seconds: float = DEFAULT_FRESH_SECONDS,
        backoff_min: float = DEFAULT_BACKOFF_MIN,
        backoff_max: float = DEFAULT_BACKOFF_MAX,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Any] = asyncio.sleep,
        connect: Any = None,
    ) -> None:
        if manager is not None:
            self._manager_provider: Callable[[], Any] = lambda: manager
        elif manager_provider is not None:
            self._manager_provider = manager_provider
        else:
            self._manager_provider = _default_manager_provider

        self.homy_ttl = float(homy_ttl)
        self.rewatch_interval = float(rewatch_interval)
        self.sweep_interval = float(sweep_interval)
        self.queue_size = int(queue_size)
        self.fresh_seconds = float(fresh_seconds)
        self.backoff_min = float(backoff_min)
        self.backoff_max = float(backoff_max)
        self._clock = clock
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._connect = connect

        self._agents: Dict[str, _AgentState] = {}
        self._subscribers: Dict[int, _Subscriber] = {}
        self._next_token = 1
        self._sweeper: Optional[asyncio.Task] = None
        self._stopping = False

    # ------------------------------------------------------------------
    # Agent provider helpers
    # ------------------------------------------------------------------

    def _manager(self):
        return self._manager_provider()

    def _agent_info(self, agent: str) -> Optional[Dict[str, Any]]:
        try:
            return self._manager().agents.get(agent)
        except Exception:  # pragma: no cover - defensive
            return None

    def _agent_known(self, agent: str) -> bool:
        try:
            return agent in self._manager().agents
        except Exception:  # pragma: no cover - defensive
            return False

    def _connect_fn(self):
        """Return the ``websockets.connect`` callable (injectable)."""
        if self._connect is not None:
            return self._connect
        import websockets

        return websockets.connect

    # ------------------------------------------------------------------
    # Hot set (refcount + sources)
    # ------------------------------------------------------------------

    def _agent_state(self, agent: str) -> _AgentState:
        state = self._agents.get(agent)
        if state is None:
            state = _AgentState(name=agent)
            self._agents[agent] = state
        return state

    def _arm_expiry(self, watch: _Watch, source: str, now: float) -> None:
        if source == SOURCE_HOMY:
            watch.expires[SOURCE_HOMY] = now + self.homy_ttl
        else:
            watch.expires.pop(source, None)

    async def subscribe(self, agent: str, container: str, source: str = SOURCE_UI) -> None:
        """Add one subscription (refcount ``source``) for *container*.

        Re-arms the source TTL when applicable and starts the per-agent WS on
        first use. No-op for an empty ref.
        """
        if not agent or not container:
            return
        state = self._agent_state(agent)
        watch = state.watches.get(container)
        if watch is None:
            watch = _Watch(container=container)
            state.watches[container] = watch
        watch.counts[source] = watch.counts.get(source, 0) + 1
        self._arm_expiry(watch, source, self._clock())
        self._ensure_runner(agent)

    async def unsubscribe(self, agent: str, container: str, source: str = SOURCE_UI) -> None:
        """Release one subscription (refcount ``source``) for *container*."""
        state = self._agents.get(agent)
        if state is None:
            return
        watch = state.watches.get(container)
        if watch is None:
            return
        remaining = watch.counts.get(source, 0) - 1
        if remaining > 0:
            watch.counts[source] = remaining
        else:
            watch.counts.pop(source, None)
            watch.expires.pop(source, None)
        if not watch.active():
            state.watches.pop(container, None)
        if state.connected:
            with contextlib.suppress(Exception):
                await self._sync_agent_watch(agent)
        self._maybe_stop_runner(agent)

    async def touch(self, agent: str, containers: List[str], source: str = SOURCE_HOMY) -> None:
        """Mark a presence for *containers* and refresh its TTL.

        Idempotent: unlike :meth:`subscribe` it never increments the refcount,
        so it is safe to call on every Homy REST request.
        """
        if not agent:
            return
        state = self._agent_state(agent)
        now = self._clock()
        added = False
        for container in containers or []:
            if not container or not isinstance(container, str):
                continue
            watch = state.watches.get(container)
            if watch is None:
                watch = _Watch(container=container)
                state.watches[container] = watch
            if watch.counts.get(source, 0) <= 0:
                watch.counts[source] = 1
                added = True
            self._arm_expiry(watch, source, now)
        if state.connected and added:
            with contextlib.suppress(Exception):
                await self._sync_agent_watch(agent)
        self._ensure_runner(agent)

    async def mark_hot_set(self, agent: str, containers: List[str]) -> None:
        """Register Homy-requested containers in the hot set (TTL 60 s)."""
        await self.touch(agent, containers, SOURCE_HOMY)

    def watched_containers(self, agent: str) -> List[str]:
        """Return the refs currently in an agent's hot set (insertion order)."""
        state = self._agents.get(agent)
        if state is None:
            return []
        return list(state.watches.keys())

    def _has_watches(self, agent: str) -> bool:
        state = self._agents.get(agent)
        return bool(state and state.watches)

    def sweep(self) -> List[str]:
        """Expire stale TTL sources; stop agents with an empty hot set.

        Returns the list of agent names that were released (all watches gone).
        """
        now = self._clock()
        released: List[str] = []
        for agent, state in list(self._agents.items()):
            for ref, watch in list(state.watches.items()):
                for source, deadline in list(watch.expires.items()):
                    if deadline <= now:
                        watch.counts.pop(source, None)
                        watch.expires.pop(source, None)
                if not watch.active():
                    state.watches.pop(ref, None)
            if not state.watches and not state.connected:
                self._purge_agent(agent)
                released.append(agent)
        return released

    # ------------------------------------------------------------------
    # Per-agent runner (WS + re-watch)
    # ------------------------------------------------------------------

    def _ensure_runner(self, agent: str) -> None:
        if self._stopping or not self._has_watches(agent):
            return
        if not self._agent_known(agent):
            # Unknown agent: keep the hot set but do not open a WS yet. A
            # later subscribe/touch once the agent is configured will start it.
            return
        state = self._agent_state(agent)
        if state.task is None or state.task.done():
            state.task = asyncio.ensure_future(self._run_agent(agent))
        self._ensure_sweeper()

    def _maybe_stop_runner(self, agent: str) -> None:
        state = self._agents.get(agent)
        if state is None:
            return
        if state.watches:
            return
        if state.task is not None and not state.task.done():
            state.task.cancel()

    def _purge_agent(self, agent: str) -> None:
        state = self._agents.pop(agent, None)
        if state is not None and state.task is not None and not state.task.done():
            state.task.cancel()

    def _ensure_sweeper(self) -> None:
        if self._sweeper is not None and not self._sweeper.done():
            return
        try:
            self._sweeper = asyncio.ensure_future(self._sweep_loop())
        except RuntimeError:  # pragma: no cover - no running loop
            self._sweeper = None

    async def _sweep_loop(self) -> None:
        try:
            while True:
                await self._sleep(self.sweep_interval)
                self.sweep()
        except asyncio.CancelledError:
            raise

    async def _run_agent(self, agent: str) -> None:
        """Maintain the agent WS with exponential backoff until it is released."""
        attempt = 0
        try:
            while not self._stopping and self._has_watches(agent):
                try:
                    await self._connect_and_stream(agent)
                    attempt = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.debug("stats stream '%s' connection failed: %s", agent, exc)
                if self._stopping or not self._has_watches(agent):
                    break
                delay = min(self.backoff_max, self.backoff_min * (2 ** attempt))
                attempt += 1
                await self._sleep(delay)
        finally:
            state = self._agents.get(agent)
            if state is not None:
                state.connected = False
                if state.task is asyncio.current_task():
                    state.task = None

    async def _connect_and_stream(self, agent: str) -> None:
        info = self._agent_info(agent)
        if info is None:
            raise RuntimeError(f"agent '{agent}' not found")
        url = _ws_url(info.get("url", "")) + "/agent/stats/stream"
        try:
            kwargs = self._manager()._agent_ws_connect_kwargs(info)
        except Exception:  # pragma: no cover - defensive
            kwargs = {}
        state = self._agent_state(agent)
        connect = self._connect_fn()
        async with connect(url, **kwargs) as ws:
            state.connected = True
            state.sent_refs = set()
            await self._sync_agent_watch(agent)
            refresh = asyncio.ensure_future(self._rewatch_loop(agent))
            try:
                async for raw in ws:
                    self._handle_message(agent, raw)
            finally:
                refresh.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await refresh
                state.connected = False

    async def _rewatch_loop(self, agent: str) -> None:
        try:
            while True:
                await self._sleep(self.rewatch_interval)
                if not self._has_watches(agent):
                    return
                try:
                    await self._sync_agent_watch(agent)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.debug("stats re-watch '%s' failed: %s", agent, exc)
        except asyncio.CancelledError:
            raise

    async def _sync_agent_watch(self, agent: str) -> None:
        """Send the desired watch set (and any explicit unwatch) to the agent."""
        state = self._agents.get(agent)
        if state is None or not state.connected:
            return
        desired: Set[str] = set(state.watches.keys())
        if desired:
            await self._post_watch(agent, sorted(desired))
        gone = state.sent_refs - desired
        if gone:
            await self._post_unwatch(agent, sorted(gone))
        state.sent_refs = desired

    async def _post_watch(self, agent: str, refs: List[str]) -> None:
        await self._manager()._request(
            agent,
            "POST",
            "/agent/stats/watch",
            json={"containers": refs},
            timeout=WATCH_HTTP_TIMEOUT,
        )

    async def _post_unwatch(self, agent: str, refs: List[str]) -> None:
        await self._manager()._request(
            agent,
            "POST",
            "/agent/stats/unwatch",
            json={"containers": refs},
            timeout=WATCH_HTTP_TIMEOUT,
        )

    # ------------------------------------------------------------------
    # Message handling / dispatch
    # ------------------------------------------------------------------

    def _handle_message(self, agent: str, raw: Any) -> None:
        """Parse one agent WS message; unknown types are ignored."""
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = raw.decode("utf-8", errors="replace")
            except Exception:
                return
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                return
        if not isinstance(raw, dict):
            return
        msg_type = raw.get("type")
        if msg_type == "snapshot":
            samples = raw.get("samples")
            if isinstance(samples, list):
                for sample in samples:
                    if isinstance(sample, dict):
                        self.ingest_sample(agent, sample, broadcast=False)
        elif msg_type == "sample":
            sample = raw.get("sample")
            if isinstance(sample, dict):
                self.ingest_sample(agent, sample, broadcast=True)
        # Any other type is intentionally ignored (forward compatibility).

    def ingest_sample(
        self, agent: str, sample: Dict[str, Any], broadcast: bool = True
    ) -> None:
        """Update caches/watch entry for a sample and fan it out to clients."""
        if not isinstance(sample, dict):
            return
        state = self._agent_state(agent)
        container_id = str(sample.get("id") or "")
        name = str(sample.get("name") or "")
        if container_id:
            state.last_samples[container_id] = sample
            if name:
                state.name_index[name] = container_id
        for watch in state.watches.values():
            if refs_match(watch.container, container_id, name) or watch.container == name:
                watch.last_sample = sample
        if broadcast:
            self._broadcast(agent, container_id, name, sample)

    def _broadcast(self, agent: str, container_id: str, name: str, sample: Dict[str, Any]) -> None:
        message = {"type": "sample", "sample": sample}
        for subscriber in list(self._subscribers.values()):
            if self._subscriber_matches(subscriber, agent, container_id, name):
                self._enqueue(subscriber, message)

    @staticmethod
    def _subscriber_matches(
        subscriber: _Subscriber, agent: str, container_id: str, name: str
    ) -> bool:
        for target_agent, ref in subscriber.targets:
            if target_agent != agent:
                continue
            if refs_match(ref, container_id, name) or ref == name:
                return True
        return False

    def _enqueue(self, subscriber: _Subscriber, message: Dict[str, Any]) -> None:
        """Enqueue without ever blocking the stream (drop oldest when full)."""
        try:
            subscriber.queue.put_nowait(message)
            return
        except asyncio.QueueFull:
            pass
        try:
            subscriber.queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        try:
            subscriber.queue.put_nowait(message)
        except asyncio.QueueFull:  # pragma: no cover - consumer race
            pass

    # ------------------------------------------------------------------
    # Frontend subscribers (WS /api/stats/stream)
    # ------------------------------------------------------------------

    def add_subscriber(self, loop: Any, queue: "asyncio.Queue") -> int:
        """Register a frontend queue; returns a token for removal."""
        token = self._next_token
        self._next_token += 1
        self._subscribers[token] = _Subscriber(loop=loop, queue=queue)
        return token

    def remove_subscriber(self, token: int) -> None:
        """Remove a subscriber and release every ``ui`` target it held."""
        subscriber = self._subscribers.pop(token, None)
        if subscriber is None:
            return
        for agent, container in list(subscriber.targets):
            try:
                asyncio.ensure_future(self.unsubscribe(agent, container, SOURCE_UI))
            except RuntimeError:  # pragma: no cover - loop closing
                pass

    async def client_subscribe(self, token: int, targets: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
        """Subscribe a frontend token to ``[(agent, container), ...]``."""
        subscriber = self._subscribers.get(token)
        if subscriber is None:
            return []
        added: List[Tuple[str, str]] = []
        for agent, container in targets:
            if not agent or not container:
                continue
            key = (agent, container)
            if key in subscriber.targets:
                continue
            subscriber.targets.add(key)
            await self.subscribe(agent, container, SOURCE_UI)
            added.append(key)
        return added

    async def client_unsubscribe(self, token: int, targets: List[Tuple[str, str]]) -> None:
        """Release frontend targets held by a token."""
        subscriber = self._subscribers.get(token)
        if subscriber is None:
            return
        for agent, container in targets:
            key = (agent, container)
            if key not in subscriber.targets:
                continue
            subscriber.targets.discard(key)
            await self.unsubscribe(agent, container, SOURCE_UI)

    # ------------------------------------------------------------------
    # Hot cache reads (integration façade)
    # ------------------------------------------------------------------

    def _find_sample(self, state: _AgentState, ref: str) -> Optional[Dict[str, Any]]:
        sample = state.last_samples.get(ref)
        if sample is not None:
            return sample
        indexed = state.name_index.get(ref)
        if indexed is not None:
            return state.last_samples.get(indexed)
        for entry in state.last_samples.values():
            if refs_match(ref, str(entry.get("id") or ""), str(entry.get("name") or "")):
                return entry
        return None

    def get_fresh_sample(
        self, agent: str, container: str, max_age_seconds: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        """Return the last sample of *container* when younger than *max_age*.

        Returns ``None`` when the container is unknown or when the sample is
        stale, so the caller falls back to a live snapshot.
        """
        state = self._agents.get(agent)
        if state is None:
            return None
        sample = self._find_sample(state, container)
        if sample is None:
            return None
        ts = sample.get("ts")
        if ts is None:
            return None
        max_age = self.fresh_seconds if max_age_seconds is None else float(max_age_seconds)
        age = self._wall_clock() * 1000.0 - float(ts)
        # A small negative age (clock skew) is still considered fresh.
        if age > max_age * 1000.0:
            return None
        return sample

    def get_samples_for_targets(self, targets: List[Tuple[str, str]]) -> List[Dict[str, Any]]:
        """Return the last known samples for a list of ``(agent, container)``."""
        samples: List[Dict[str, Any]] = []
        seen: Set[int] = set()
        for agent, container in targets:
            state = self._agents.get(agent)
            if state is None:
                continue
            watch = state.watches.get(container)
            sample = watch.last_sample if watch is not None else None
            if sample is None:
                sample = self._find_sample(state, container)
            if sample is None or id(sample) in seen:
                continue
            seen.add(id(sample))
            samples.append(sample)
        return samples

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """No-op hook kept symmetrical with the agent manager lifecycle."""
        self._stopping = False

    def stop(self) -> None:
        """Cancel every runner/sweeper and clear subscribers and hot set."""
        self._stopping = True
        for state in list(self._agents.values()):
            if state.task is not None and not state.task.done():
                state.task.cancel()
        self._agents.clear()
        if self._sweeper is not None and not self._sweeper.done():
            self._sweeper.cancel()
        self._sweeper = None
        self._subscribers.clear()


# ---------------------------------------------------------------------------
# Process-wide singleton + thin façade
# ---------------------------------------------------------------------------

_manager: Optional[AgentStatsStreamManager] = None


def get_manager() -> AgentStatsStreamManager:
    """Return the process-wide manager, creating it lazily."""
    global _manager
    if _manager is None:
        _manager = AgentStatsStreamManager()
    return _manager


def set_manager(manager: Optional[AgentStatsStreamManager]) -> None:
    """Replace the process-wide manager (tests / advanced wiring)."""
    global _manager
    _manager = manager


def start() -> None:
    get_manager().start()


def stop() -> None:
    get_manager().stop()


async def subscribe(agent: str, container: str, source: str = SOURCE_UI) -> None:
    await get_manager().subscribe(agent, container, source)


async def unsubscribe(agent: str, container: str, source: str = SOURCE_UI) -> None:
    await get_manager().unsubscribe(agent, container, source)


async def mark_hot_set(agent: str, containers: List[str]) -> None:
    await get_manager().mark_hot_set(agent, containers)
