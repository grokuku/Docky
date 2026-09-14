"""Real-time container stats streaming for the Docky agent (LOT 1 — agent side).

Architecture
------------
``docker stats`` has no native batch/stream API: each container costs one
persistent ``container.stats(stream=True)`` HTTP call to the daemon.  This
module owns that cost and exposes a small, self-contained surface used by
``agent.routes``:

- a **watch set** (``watch`` / ``unwatch`` / ``watched``): only the containers
  the orchestrator explicitly subscribes to are streamed. Entries expire
  automatically after :data:`DEFAULT_TTL_SECONDS` without a refresh
  (idempotent ``watch`` calls re-arm them), so a disconnected orchestrator
  cannot leave streamers running forever;
- one :class:`StatsStreamer` **daemon thread per watched container** holding a
  persistent docker-py ``stats(stream=True, decode=True)`` subscription;
- a per-container **ring buffer** (:data:`RING_BUFFER_SIZE` samples, ~15 min at
  1 s/sample) for the recent, fine-grained history;
- a **downsampled SQLite persistence** (1 point / 30 s by default, retention
  ~24 h) written by a dedicated daemon thread so streamers never block on I/O;
- a **broadcast** of every live sample to the WS subscribers registered by
  ``GET /agent/stats/stream``.

Sample payload (trimmed — no per-CPU / per-interface arrays)::

    {"ts": 1700000000000, "id": "a1b2c3d4e5f6", "name": "web",
     "state": "running", "cpu_percent": 42.1, "cpu_count": 4,
     "mem_usage": 12345678, "mem_limit": 1073741824, "mem_percent": 1.15,
     "mem_cache": 4096, "net_rx": 1234, "net_tx": 5678}

``cpu_percent`` is the **raw multi-core** percentage returned by the Docker API
(same convention as :func:`agent.docker_manager.get_container_stats`); the
integration façade normalises it to host 0–100.

Environment variables
---------------------
- ``DOCKY_STATS_RETENTION_HOURS``  (default :data:`DEFAULT_RETENTION_HOURS`)
- ``DOCKY_STATS_DOWNSAMPLE_SECONDS`` (default :data:`DEFAULT_DOWNSAMPLE_SECONDS`)

No automatic re-arm of the watch set at boot: if nobody reconnects, nothing is
streamed.  See ``docs/stats-streaming.md``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from docker.errors import APIError, DockerException, NotFound

from agent import docker_manager
from agent.config import get_data_dir

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Watch-set TTL: an entry not refreshed by a ``watch`` call within this delay
#: is expired and its streamer stopped.
DEFAULT_TTL_SECONDS = 30.0

#: Background sweep cadence (deliberately low: no aggressive busy loop).
DEFAULT_SWEEP_INTERVAL_SECONDS = 5.0

#: Ring buffer capacity per watched container (~15 min at 1 sample/s).
RING_BUFFER_SIZE = 900

#: Downsampled persistence: one point every N seconds.
DEFAULT_DOWNSAMPLE_SECONDS = 30.0

#: Persisted history retention (hours).  Purge runs opportunistically.
DEFAULT_RETENTION_HOURS = 24.0

#: How long a streamer waits before reconnecting after a stream ends/errors.
STREAM_RETRY_SECONDS = 2.0

#: Supported history windows (seconds): 15 min / 1 h / 24 h.
SUPPORTED_WINDOWS: Tuple[int, ...] = (900, 3600, 86400)

#: Interval between two retention purges.
PURGE_INTERVAL_SECONDS = 300.0


def _env_positive_number(name: str, default: float) -> float:
    """Read a strictly positive float from ``name``, else fall back to *default*."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return float(default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r, falling back to %s", name, raw, default)
        return float(default)
    if value <= 0:
        logger.warning("Non-positive %s=%r, falling back to %s", name, raw, default)
        return float(default)
    return value


# ---------------------------------------------------------------------------
# Sample helpers
# ---------------------------------------------------------------------------

def _container_identity(container) -> Tuple[str, str]:
    """Return ``(short_id, name)`` for a docker-py container.

    ``short_id`` is used as the canonical key (it matches the ``id`` field of
    the existing one-shot stats payload).  ``name`` is the docker-py name
    without its leading ``/``.
    """
    cid = getattr(container, "short_id", None)
    if not cid:
        cid = (getattr(container, "id", "") or "")[:12]
    name = (getattr(container, "name", "") or "").lstrip("/")
    return str(cid), name


def build_sample(container, stats: Dict[str, Any], ts: Optional[int] = None) -> Dict[str, Any]:
    """Build a trimmed live sample from a raw Docker ``stats`` dict.

    Reuses :func:`agent.docker_manager._stats_from_raw` so the streamed
    counters are computed with the exact same formulas as the one-shot stats
    endpoint (``cpu_percent`` raw multi-core, memory ``usage - cache``,
    aggregated network).  ``net_rx``/``net_tx`` are the trimmed names of the
    internal ``network_rx``/``network_tx`` fields.
    """
    payload = docker_manager._stats_from_raw(stats)
    cid, name = _container_identity(container)
    return {
        "ts": int(ts if ts is not None else time.time() * 1000),
        "id": cid,
        "name": name,
        "state": getattr(container, "status", "unknown") or "unknown",
        "cpu_percent": payload["cpu_percent"],
        "cpu_count": payload["cpu_count"],
        "mem_usage": payload["mem_usage"],
        "mem_limit": payload["mem_limit"],
        "mem_percent": payload["mem_percent"],
        "mem_cache": payload["mem_cache"],
        "net_rx": payload["network_rx"],
        "net_tx": payload["network_tx"],
    }


def _history_point(sample: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce a full sample to the 7 history fields exposed by the API."""
    return {
        "ts": int(sample.get("ts") or 0),
        "cpu_percent": sample.get("cpu_percent", 0.0),
        "mem_usage": sample.get("mem_usage", 0),
        "mem_limit": sample.get("mem_limit", 0),
        "mem_percent": sample.get("mem_percent", 0.0),
        "net_rx": sample.get("net_rx", 0),
        "net_tx": sample.get("net_tx", 0),
    }


# ---------------------------------------------------------------------------
# SQLite persistence (downsampled + retention purge)
# ---------------------------------------------------------------------------

class StatsStore:
    """Downsampled history persistence backed by ``<data_dir>/stats_history.db``.

    Schema::

        samples(container_id, ts, cpu_percent, mem_usage, mem_limit,
                mem_percent, net_rx, net_tx, PRIMARY KEY (container_id, ts))
        INDEX idx_samples_container_ts ON samples (container_id, ts)

    Writes are enqueued by :meth:`maybe_record` and flushed by a dedicated
    daemon thread, so the per-container streamers never block on disk I/O.
    The ``(container_id, ts)`` primary key plus the interval gate guarantee
    there can never be a duplicated point.
    """

    def __init__(
        self,
        db_path: Optional[Any] = None,
        retention_hours: Optional[float] = None,
        downsample_seconds: Optional[float] = None,
        purge_interval: float = PURGE_INTERVAL_SECONDS,
    ) -> None:
        if db_path is None:
            self.db_path = str(get_data_dir() / "stats_history.db")
        else:
            self.db_path = str(db_path)
        self.retention_hours = float(
            retention_hours
            if retention_hours is not None
            else _env_positive_number("DOCKY_STATS_RETENTION_HOURS", DEFAULT_RETENTION_HOURS)
        )
        self.downsample_seconds = float(
            downsample_seconds
            if downsample_seconds is not None
            else _env_positive_number("DOCKY_STATS_DOWNSAMPLE_SECONDS", DEFAULT_DOWNSAMPLE_SECONDS)
        )
        self.purge_interval = float(purge_interval)

        self._lock = threading.RLock()
        self._last_ts: Dict[str, int] = {}
        self._queue: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._conn: Optional[sqlite3.Connection] = None
        self._last_purge = 0.0
        self._init_db()

    # -- connection / schema ------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self.db_path, check_same_thread=False, timeout=10.0
        )
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            # In-memory / restricted FS: WAL is best-effort only.
            pass
        return conn

    def _init_db(self) -> None:
        with self._lock:
            if self._conn is None:
                self._conn = self._connect()
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS samples (
                    container_id TEXT    NOT NULL,
                    ts           INTEGER NOT NULL,
                    cpu_percent  REAL    NOT NULL,
                    mem_usage    INTEGER NOT NULL,
                    mem_limit    INTEGER NOT NULL,
                    mem_percent  REAL    NOT NULL,
                    net_rx       INTEGER NOT NULL,
                    net_tx       INTEGER NOT NULL,
                    PRIMARY KEY (container_id, ts)
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_samples_container_ts "
                "ON samples (container_id, ts)"
            )
            self._conn.commit()

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Start the background writer thread (idempotent)."""
        with self._lock:
            if self._conn is None:
                self._init_db()
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._writer_loop, name="docky-stats-store", daemon=True
            )
            self._thread.start()

    def close(self) -> None:
        """Drain the queue, stop the writer thread and close the connection."""
        thread = self._thread
        if thread is not None:
            # Sentinel placed after already-enqueued samples: the writer drains
            # them in FIFO order before exiting.
            self._queue.put(None)
            thread.join(timeout=3.0)
            self._thread = None
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None

    # -- writes -------------------------------------------------------------

    def record(self, sample: Dict[str, Any]) -> None:
        """Insert one point synchronously (used by the writer thread/tests)."""
        cid = str(sample.get("id") or "")
        if not cid:
            return
        row = (
            cid,
            int(sample.get("ts") or 0),
            float(sample.get("cpu_percent", 0.0) or 0.0),
            int(sample.get("mem_usage", 0) or 0),
            int(sample.get("mem_limit", 0) or 0),
            float(sample.get("mem_percent", 0.0) or 0.0),
            int(sample.get("net_rx", 0) or 0),
            int(sample.get("net_tx", 0) or 0),
        )
        with self._lock:
            if self._conn is None:
                return
            self._conn.execute(
                "INSERT OR REPLACE INTO samples "
                "(container_id, ts, cpu_percent, mem_usage, mem_limit, "
                " mem_percent, net_rx, net_tx) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            self._conn.commit()

    def maybe_record(self, sample: Dict[str, Any]) -> bool:
        """Downsampling gate: persist *sample* only if the interval elapsed.

        Returns ``True`` when the sample was accepted, ``False`` when it was
        skipped because the previous point is younger than
        :attr:`downsample_seconds` (no duplicate is ever written).
        """
        cid = str(sample.get("id") or "")
        if not cid:
            return False
        ts = int(sample.get("ts") or 0)
        interval_ms = int(self.downsample_seconds * 1000)
        with self._lock:
            last = self._last_ts.get(cid)
            if last is not None and ts - last < interval_ms:
                return False
            self._last_ts[cid] = ts
            has_writer = self._thread is not None
        # Enqueue when the background writer is running; otherwise write
        # synchronously so callers/tests without a started store still persist.
        if has_writer:
            self._queue.put_nowait(dict(sample))
        else:
            self.record(sample)
        return True

    # -- reads --------------------------------------------------------------

    def query(
        self,
        container_id: str,
        since_ms: int,
        until_ms: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return persisted points for *container_id* in ``[since, until]``."""
        sql = (
            "SELECT ts, cpu_percent, mem_usage, mem_limit, mem_percent, "
            "net_rx, net_tx FROM samples WHERE container_id = ? AND ts >= ?"
        )
        params: List[Any] = [container_id, int(since_ms)]
        if until_ms is not None:
            sql += " AND ts <= ?"
            params.append(int(until_ms))
        sql += " ORDER BY ts"
        with self._lock:
            if self._conn is None:
                return []
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [
            {
                "ts": r[0],
                "cpu_percent": r[1],
                "mem_usage": r[2],
                "mem_limit": r[3],
                "mem_percent": r[4],
                "net_rx": r[5],
                "net_tx": r[6],
            }
            for r in rows
        ]

    def purge(self, now_ms: Optional[int] = None) -> int:
        """Delete points older than the retention window; return rows removed."""
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        cutoff = int(now_ms - self.retention_hours * 3600 * 1000)
        with self._lock:
            if self._conn is None:
                return 0
            cursor = self._conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            self._conn.commit()
            return cursor.rowcount

    def count(self, container_id: Optional[str] = None) -> int:
        """Return the number of persisted points (for tests/observability)."""
        with self._lock:
            if self._conn is None:
                return 0
            if container_id is None:
                row = self._conn.execute("SELECT COUNT(*) FROM samples").fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM samples WHERE container_id = ?",
                    (container_id,),
                ).fetchone()
        return int(row[0]) if row else 0

    # -- background writer --------------------------------------------------

    def flush(self, timeout: float = 2.0) -> None:
        """Block until the writer thread has drained the current queue."""
        if self._thread is None:
            return
        deadline = time.monotonic() + max(0.0, timeout)
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)

    def _writer_loop(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                self._maybe_purge()
                continue
            try:
                if item is None:
                    return
                self.record(item)
            except Exception:  # pragma: no cover - defensive
                logger.exception("stats history write failed")
            finally:
                self._queue.task_done()
            self._maybe_purge()

    def _maybe_purge(self) -> None:
        now = time.monotonic()
        if now - self._last_purge < self.purge_interval:
            return
        self._last_purge = now
        try:
            self.purge()
        except Exception:  # pragma: no cover - defensive
            logger.exception("stats history purge failed")


# ---------------------------------------------------------------------------
# Per-container streamer
# ---------------------------------------------------------------------------

class StatsStreamer:
    """Persistent ``container.stats(stream=True)`` subscription for one container.

    Runs a daemon thread that keeps the docker stream open, rebuilds a trimmed
    sample per tick and hands it to *on_sample*.  On stream end/error it
    reconnects after :data:`STREAM_RETRY_SECONDS` (bounded, no busy loop) until
    :meth:`stop` is called.
    """

    def __init__(
        self,
        container_id: str,
        name: str,
        on_sample,
        retry_seconds: float = STREAM_RETRY_SECONDS,
    ) -> None:
        self.container_id = container_id
        self.name = name
        self._on_sample = on_sample
        self._retry_seconds = float(retry_seconds)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._gen = None
        self._gen_lock = threading.Lock()

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"docky-stats-{str(self.container_id)[:12]}",
            daemon=True,
        )
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the thread to stop and release the Docker stream."""
        self._stop.set()
        with self._gen_lock:
            gen = self._gen
        if gen is not None:
            try:
                gen.close()
            except Exception:
                # ``close()`` may raise if the generator is executing in the
                # streamer thread; the stop flag still bounds the exit.
                pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    # -- worker -------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            container = None
            gen = None
            try:
                client = docker_manager.get_docker_client()
                container = client.containers.get(self.container_id)
                gen = container.stats(stream=True, decode=True)
            except (NotFound, DockerException, APIError) as exc:
                logger.debug(
                    "stats streamer %s: container unavailable: %s", self.container_id, exc
                )
                if self._stop.wait(self._retry_seconds):
                    break
                continue
            except Exception:
                logger.exception(
                    "stats streamer %s: unexpected error opening stream", self.container_id
                )
                if self._stop.wait(self._retry_seconds):
                    break
                continue

            with self._gen_lock:
                self._gen = gen
            try:
                for raw in gen:
                    if self._stop.is_set():
                        break
                    stats = raw
                    if isinstance(stats, (bytes, bytearray)):
                        try:
                            stats = json.loads(stats.decode("utf-8", errors="replace"))
                        except (ValueError, TypeError):
                            continue
                    if not isinstance(stats, dict):
                        continue
                    sample = build_sample(container, stats)
                    try:
                        self._on_sample(sample)
                    except Exception:  # pragma: no cover - defensive
                        logger.exception(
                            "stats streamer %s: sample handler failed", self.container_id
                        )
            except (NotFound, DockerException, APIError) as exc:
                logger.debug("stats streamer %s: stream ended: %s", self.container_id, exc)
            except Exception:
                logger.exception("stats streamer %s: stream failed", self.container_id)
            finally:
                with self._gen_lock:
                    self._gen = None
                if gen is not None:
                    try:
                        gen.close()
                    except Exception:
                        pass

            if self._stop.is_set():
                break
            self._stop.wait(self._retry_seconds)


# ---------------------------------------------------------------------------
# Watch entry
# ---------------------------------------------------------------------------

@dataclass
class _WatchEntry:
    id: str
    name: str
    last_seen: float = 0.0
    last_sample: Optional[Dict[str, Any]] = None
    streamer: Optional[StatsStreamer] = None
    ring: Deque[Dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=RING_BUFFER_SIZE)
    )


def _queue_put_latest(q: "asyncio.Queue", item: Any) -> None:
    """Put *item* on *q*, dropping the oldest entry when full.

    Runs on the subscriber's event loop (scheduled via
    ``loop.call_soon_threadsafe``); never blocks the streamer thread.
    """
    try:
        q.put_nowait(item)
        return
    except asyncio.QueueFull:
        pass
    try:
        q.get_nowait()
    except asyncio.QueueEmpty:
        return
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:  # pragma: no cover - concurrent consumer race
        pass


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class StatsStreamManager:
    """Owns the watch set, the streamers, the store and the WS subscribers."""

    def __init__(
        self,
        store: Optional[StatsStore] = None,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        sweep_interval: float = DEFAULT_SWEEP_INTERVAL_SECONDS,
        ring_size: int = RING_BUFFER_SIZE,
        retry_seconds: float = STREAM_RETRY_SECONDS,
    ) -> None:
        self._store = store if store is not None else StatsStore()
        self.ttl_seconds = float(ttl_seconds)
        self.sweep_interval = float(sweep_interval)
        self.ring_size = int(ring_size)
        self.retry_seconds = float(retry_seconds)

        self._lock = threading.RLock()
        self._watched: Dict[str, _WatchEntry] = {}
        self._subscribers: Dict[int, Tuple[Any, "asyncio.Queue"]] = {}
        self._next_sub = 1
        self._sweep_stop = threading.Event()
        self._sweep_thread: Optional[threading.Thread] = None

    # -- lifecycle ----------------------------------------------------------

    @property
    def store(self) -> StatsStore:
        return self._store

    def start(self) -> None:
        """Start the store writer and the watch-set sweeper (idempotent)."""
        self._store.start()
        with self._lock:
            if self._sweep_thread is not None and self._sweep_thread.is_alive():
                return
            self._sweep_stop.clear()
            self._sweep_thread = threading.Thread(
                target=self._sweep_loop, name="docky-stats-sweeper", daemon=True
            )
            self._sweep_thread.start()

    def stop(self) -> None:
        """Stop every streamer and the sweeper, clear watches/subscribers.

        The store stays open (call :meth:`close` for a full shutdown).
        """
        with self._lock:
            entries = list(self._watched.values())
            self._watched.clear()
            self._subscribers.clear()
        for entry in entries:
            self._stop_entry(entry)
        self._sweep_stop.set()
        thread = self._sweep_thread
        if thread is not None:
            thread.join(timeout=3.0)
            self._sweep_thread = None

    def close(self) -> None:
        """Full shutdown: :meth:`stop` + close the persistence."""
        self.stop()
        self._store.close()

    # -- watch set ----------------------------------------------------------

    def _resolve(self, ref: str) -> Optional[Tuple[str, str]]:
        """Resolve a container id/name via Docker; ``None`` when unknown."""
        try:
            client = docker_manager.get_docker_client()
            container = client.containers.get(ref)
        except (NotFound, DockerException, APIError):
            return None
        except Exception:
            logger.exception("stats watch: failed to resolve %r", ref)
            return None
        cid, name = _container_identity(container)
        if not cid:
            return None
        return cid, name

    def resolve_container(self, ref: str) -> Optional[Tuple[str, str]]:
        """Public resolution helper for the history route.

        Prefers the in-memory watch registry (works even if Docker is
        unreachable for an already-watched container), then falls back to a
        Docker lookup.
        """
        with self._lock:
            cid = self._canonical_locked(ref)
            entry = self._watched.get(cid)
            if entry is not None:
                return entry.id, entry.name
        return self._resolve(ref)

    def watch(self, containers: List[str]) -> List[str]:
        """Add *containers* to the watch set and re-arm their TTL (idempotent).

        Unknown refs are skipped.  Returns the canonical short ids actually
        watched (deduplicated).
        """
        refs = [c for c in (containers or []) if isinstance(c, str) and c]
        accepted: List[str] = []
        now = time.monotonic()
        for ref in refs:
            resolved = self._resolve(ref)
            if resolved is None:
                logger.debug("stats watch: unknown container %r", ref)
                continue
            cid, name = resolved
            with self._lock:
                entry = self._watched.get(cid)
                if entry is None:
                    entry = _WatchEntry(
                        id=cid, name=name, ring=deque(maxlen=self.ring_size)
                    )
                    self._watched[cid] = entry
                elif name:
                    entry.name = name
                entry.last_seen = now
                if entry.streamer is None or not entry.streamer.is_alive():
                    entry.streamer = self._spawn_streamer(entry)
                if cid not in accepted:
                    accepted.append(cid)
        return accepted

    def unwatch(self, containers: List[str]) -> List[str]:
        """Remove *containers* (id or name) from the watch set.

        Returns the canonical ids actually removed.
        """
        refs = [c for c in (containers or []) if isinstance(c, str) and c]
        removed: List[str] = []
        entries: List[_WatchEntry] = []
        with self._lock:
            for ref in refs:
                cid = self._canonical_locked(ref)
                entry = self._watched.pop(cid, None)
                if entry is not None:
                    if cid not in removed:
                        removed.append(cid)
                    entries.append(entry)
        for entry in entries:
            self._stop_entry(entry)
        return removed

    def watched(self) -> List[str]:
        """Return the canonical ids currently watched (insertion order)."""
        with self._lock:
            return [entry.id for entry in self._watched.values()]

    def watch_info(self) -> List[Dict[str, Any]]:
        """Observability helper: ``[{id, name, age, alive}, ...]``."""
        now = time.monotonic()
        with self._lock:
            return [
                {
                    "id": entry.id,
                    "name": entry.name,
                    "age": round(now - entry.last_seen, 3),
                    "alive": bool(entry.streamer and entry.streamer.is_alive()),
                }
                for entry in self._watched.values()
            ]

    def _canonical_locked(self, ref: str) -> str:
        """Return the canonical id for *ref* (id or name) under the lock."""
        if ref in self._watched:
            return ref
        for cid, entry in self._watched.items():
            if entry.name == ref:
                return cid
        return ref

    def _spawn_streamer(self, entry: _WatchEntry) -> StatsStreamer:
        streamer = StatsStreamer(
            entry.id,
            entry.name,
            self._handle_sample,
            retry_seconds=self.retry_seconds,
        )
        streamer.start()
        return streamer

    def _stop_entry(self, entry: _WatchEntry) -> None:
        streamer = entry.streamer
        entry.streamer = None
        if streamer is not None:
            streamer.stop()

    # -- sweep / TTL --------------------------------------------------------

    def sweep(self) -> List[str]:
        """Expire stale watch entries and revive dead streamers.

        Returns the list of expired container ids.
        """
        now = time.monotonic()
        expired: List[_WatchEntry] = []
        with self._lock:
            for cid, entry in list(self._watched.items()):
                if now - entry.last_seen > self.ttl_seconds:
                    expired.append(self._watched.pop(cid))
                    continue
                if entry.streamer is None or not entry.streamer.is_alive():
                    entry.streamer = self._spawn_streamer(entry)
        for entry in expired:
            self._stop_entry(entry)
        return [entry.id for entry in expired]

    def _sweep_loop(self) -> None:
        while not self._sweep_stop.wait(self.sweep_interval):
            try:
                self.sweep()
            except Exception:  # pragma: no cover - defensive
                logger.exception("stats sweep failed")

    # -- samples ------------------------------------------------------------

    def _handle_sample(self, sample: Dict[str, Any]) -> None:
        cid = sample.get("id")
        with self._lock:
            entry = self._watched.get(cid)
            if entry is None:
                return
            entry.ring.append(sample)
            entry.last_sample = sample
        try:
            self._store.maybe_record(sample)
        except Exception:  # pragma: no cover - defensive
            logger.exception("stats store record failed")
        self._broadcast(sample)

    def snapshot(self) -> List[Dict[str, Any]]:
        """Return the last known sample of every watched container."""
        with self._lock:
            return [
                entry.last_sample
                for entry in self._watched.values()
                if entry.last_sample is not None
            ]

    # -- subscribers / broadcast -------------------------------------------

    def add_subscriber(self, loop, q: "asyncio.Queue") -> int:
        """Register a WS queue; returns a token for :meth:`remove_subscriber`."""
        with self._lock:
            token = self._next_sub
            self._next_sub += 1
            self._subscribers[token] = (loop, q)
        return token

    def remove_subscriber(self, token: int) -> bool:
        with self._lock:
            return self._subscribers.pop(token, None) is not None

    def _broadcast(self, sample: Dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers.items())
        for token, (loop, q) in subscribers:
            try:
                loop.call_soon_threadsafe(_queue_put_latest, q, sample)
            except RuntimeError:
                # Event loop closed (client disconnected at shutdown).
                self.remove_subscriber(token)
            except Exception:  # pragma: no cover - defensive
                logger.debug("stats broadcast failed for subscriber %s", token)

    # -- history ------------------------------------------------------------

    def get_history(self, container: str, window_seconds: int) -> List[Dict[str, Any]]:
        """Merged time series (memory ring + persisted) for a window.

        Supported windows: :data:`SUPPORTED_WINDOWS`.  ``ValueError`` otherwise.
        The memory ring (fine, ~1 s) overrides the downsampled persisted points
        (30 s) when both cover a timestamp.
        """
        try:
            window = int(window_seconds)
        except (TypeError, ValueError):
            raise ValueError(f"unsupported window: {window_seconds!r}")
        if window not in SUPPORTED_WINDOWS:
            raise ValueError(
                f"unsupported window: {window}; supported: {list(SUPPORTED_WINDOWS)}"
            )

        with self._lock:
            cid = self._canonical_locked(container)
            entry = self._watched.get(cid)
            ring = list(entry.ring) if entry is not None else []

        now_ms = int(time.time() * 1000)
        since_ms = now_ms - window * 1000

        merged: Dict[int, Dict[str, Any]] = {}
        for point in self._store.query(cid, since_ms):
            merged[int(point["ts"])] = _history_point(point)
        for sample in ring:
            ts = int(sample.get("ts") or 0)
            if ts >= since_ms:
                merged[ts] = _history_point(sample)
        return [merged[ts] for ts in sorted(merged)]


# ---------------------------------------------------------------------------
# Module-level singleton + thin façade
# ---------------------------------------------------------------------------

_manager: Optional[StatsStreamManager] = None
_manager_lock = threading.Lock()


def get_manager() -> StatsStreamManager:
    """Return the process-wide manager, creating it lazily."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = StatsStreamManager()
        return _manager


def set_manager(manager: Optional[StatsStreamManager]) -> None:
    """Replace the process-wide manager (tests / advanced wiring)."""
    global _manager
    with _manager_lock:
        _manager = manager


def start() -> None:
    get_manager().start()


def shutdown() -> None:
    get_manager().close()


def stop() -> None:
    get_manager().stop()


def watch(containers: List[str]) -> List[str]:
    return get_manager().watch(containers)


def unwatch(containers: List[str]) -> List[str]:
    return get_manager().unwatch(containers)


def watched() -> List[str]:
    return get_manager().watched()


def watch_info() -> List[Dict[str, Any]]:
    return get_manager().watch_info()


def snapshot() -> List[Dict[str, Any]]:
    return get_manager().snapshot()


def resolve_container(ref: str) -> Optional[Tuple[str, str]]:
    return get_manager().resolve_container(ref)


def get_history(container: str, window_seconds: int) -> List[Dict[str, Any]]:
    return get_manager().get_history(container, window_seconds)


def add_subscriber(loop, q: "asyncio.Queue") -> int:
    return get_manager().add_subscriber(loop, q)


def remove_subscriber(token: int) -> bool:
    return get_manager().remove_subscriber(token)
