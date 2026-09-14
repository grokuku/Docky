"""Tests for ``agent.stats_stream`` (LOT 1 — real-time stats streaming).

Hermetic: no Docker daemon, no network, no real ``data`` directory. Fake
docker-py containers expose a controllable ``stats(stream=True, decode=True)``
generator; SQLite stores live in ``tmp_path``.
"""

import asyncio
import threading
import time
from collections import deque

import pytest

import docker

from agent import docker_manager as dm
from agent import stats_stream
from agent.stats_stream import StatsStore, StatsStreamManager, _WatchEntry


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeStatsContainer:
    """Minimal docker-py container with a controllable streaming ``stats()``."""

    def __init__(self, cid="abcdef123456", name="web", status="running",
                 samples=None, stream_factory=None):
        self.id = cid
        self.short_id = cid[:12]
        self.name = name
        self.status = status
        self.attrs = {"Config": {"Labels": {}}, "State": {}, "Created": ""}
        self.ports = {}
        self._samples = list(samples or [])
        self._stream_factory = stream_factory
        self.stats_calls = []

    def stats(self, stream=False, decode=True, **kwargs):
        self.stats_calls.append({"stream": stream, "decode": decode})
        if not stream:
            return self._samples[0] if self._samples else {}
        if self._stream_factory is not None:
            return self._stream_factory()
        return iter(list(self._samples))


class _FakeContainers:
    def __init__(self, containers):
        self._by_id = {c.id: c for c in containers}
        self._by_name = {c.name: c for c in containers}
        self._by_short = {c.short_id: c for c in containers}

    def get(self, ref):
        for mapping in (self._by_id, self._by_name, self._by_short):
            if ref in mapping:
                return mapping[ref]
        raise docker.errors.NotFound(f"container {ref} not found")

    def list(self, all=True):
        return list(self._by_id.values())


class FakeDockerClient:
    def __init__(self, containers=None):
        self.containers = _FakeContainers(list(containers or []))


def raw_stats(cpu_delta=100, system_delta=100, online_cpus=2, mem_usage=1000,
              mem_limit=10000, cache=400, rx=10, tx=20):
    return {
        "cpu_stats": {
            "cpu_usage": {"total_usage": cpu_delta + 100},
            "system_cpu_usage": system_delta + 900,
            "online_cpus": online_cpus,
        },
        "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 900},
        "memory_stats": {
            "usage": mem_usage,
            "limit": mem_limit,
            "stats": {"inactive_file": cache},
        },
        "networks": {"eth0": {"rx_bytes": rx, "tx_bytes": tx}},
    }


def _sample(cid="abcdef123456", name="web", ts=0, cpu_percent=1.0):
    return {
        "ts": ts,
        "id": cid,
        "name": name,
        "state": "running",
        "cpu_percent": cpu_percent,
        "cpu_count": 2,
        "mem_usage": 600,
        "mem_limit": 10000,
        "mem_percent": 6.0,
        "mem_cache": 400,
        "net_rx": 1,
        "net_tx": 2,
    }


def _empty_gen():
    return iter(())


def _infinite_gen(interval=0.005):
    def _gen():
        while True:
            yield raw_stats()
            time.sleep(interval)
    return _gen


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _inject_entry(manager, cid, name, ring=None, last_sample=None):
    """Insert a watch entry directly (no docker/streamer) for focused tests."""
    entry = _WatchEntry(
        id=cid,
        name=name,
        ring=deque(ring or [], maxlen=manager.ring_size),
        last_sample=last_sample,
        last_seen=time.monotonic(),
    )
    with manager._lock:
        manager._watched[cid] = entry
    return entry


@pytest.fixture
def make_manager(tmp_path, monkeypatch):
    """Factory building a manager backed by a fake docker client + tmp SQLite."""
    created = []

    def _factory(containers=None, **kwargs):
        client = FakeDockerClient(containers or [])
        monkeypatch.setattr(dm, "get_docker_client", lambda: client)
        store = kwargs.pop("store", None)
        if store is None:
            store = StatsStore(db_path=tmp_path / f"history{len(created)}.db")
        manager = StatsStreamManager(store=store, sweep_interval=0.05, **kwargs)
        created.append(manager)
        return manager

    yield _factory

    for manager in created:
        try:
            manager.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Payload (trimmed)
# ---------------------------------------------------------------------------

def test_build_sample_has_exactly_trimmed_keys():
    container = FakeStatsContainer()
    sample = stats_stream.build_sample(container, raw_stats(), ts=123)
    assert set(sample) == {
        "ts", "id", "name", "state", "cpu_percent", "cpu_count", "mem_usage",
        "mem_limit", "mem_percent", "mem_cache", "net_rx", "net_tx",
    }
    assert sample["ts"] == 123
    assert sample["id"] == "abcdef123456"
    assert sample["name"] == "web"
    assert sample["state"] == "running"
    assert sample["cpu_percent"] == 200.0
    assert sample["cpu_count"] == 2
    assert sample["mem_usage"] == 600
    assert sample["mem_limit"] == 10000
    assert sample["mem_percent"] == 6.0
    assert sample["mem_cache"] == 400
    assert sample["net_rx"] == 10
    assert sample["net_tx"] == 20
    # No per-CPU / per-interface arrays leaked.
    assert "cpu_stats" not in sample
    assert "networks" not in sample


def test_build_sample_handles_malformed_stats():
    sample = stats_stream.build_sample(FakeStatsContainer(), "not-a-dict", ts=1)
    assert sample["cpu_percent"] == 0.0
    assert sample["cpu_count"] == 1
    assert sample["mem_usage"] == 0
    assert sample["net_rx"] == 0


# ---------------------------------------------------------------------------
# Downsampling
# ---------------------------------------------------------------------------

def test_maybe_record_one_point_per_interval(tmp_path):
    store = StatsStore(db_path=tmp_path / "down.db", downsample_seconds=30)
    try:
        assert store.maybe_record(_sample(ts=0)) is True
        assert store.maybe_record(_sample(ts=1000)) is False
        assert store.maybe_record(_sample(ts=29999)) is False
        assert store.maybe_record(_sample(ts=30000)) is True
        assert store.maybe_record(_sample(ts=30000)) is False  # no duplicate
        assert store.count("abcdef123456") == 2
    finally:
        store.close()


def test_maybe_record_ignores_missing_id(tmp_path):
    store = StatsStore(db_path=tmp_path / "noid.db")
    try:
        assert store.maybe_record({"ts": 1}) is False
        assert store.count() == 0
    finally:
        store.close()


def test_store_writer_thread_persists_and_flushes(tmp_path):
    store = StatsStore(db_path=tmp_path / "writer.db", downsample_seconds=30)
    store.start()
    try:
        now = int(time.time() * 1000)
        assert store.maybe_record(_sample(ts=now)) is True
        store.flush()
        assert store.count("abcdef123456") == 1
    finally:
        store.close()


# ---------------------------------------------------------------------------
# SQLite persistence (read by window + retention purge)
# ---------------------------------------------------------------------------

def test_store_query_by_window_and_ordering(tmp_path):
    store = StatsStore(db_path=tmp_path / "q.db")
    now = 1_000_000_000_000
    store.record(_sample(cid="a", ts=now - 10_000, cpu_percent=1.0))
    store.record(_sample(cid="a", ts=now - 5_000, cpu_percent=2.0))
    store.record(_sample(cid="a", ts=now - 2_000_000, cpu_percent=3.0))
    store.record(_sample(cid="b", ts=now, cpu_percent=9.0))
    try:
        points = store.query("a", now - 60_000)
        assert [p["ts"] for p in points] == [now - 10_000, now - 5_000]
        assert set(points[0]) == {
            "ts", "cpu_percent", "mem_usage", "mem_limit",
            "mem_percent", "net_rx", "net_tx",
        }
    finally:
        store.close()


def test_store_primary_key_dedupes(tmp_path):
    store = StatsStore(db_path=tmp_path / "dup.db")
    try:
        store.record(_sample(ts=500))
        store.record(_sample(ts=500, cpu_percent=99.0))
        assert store.count("abcdef123456") == 1
        assert store.query("abcdef123456", 0)[0]["cpu_percent"] == 99.0
    finally:
        store.close()


def test_store_purge_removes_beyond_retention(tmp_path):
    store = StatsStore(db_path=tmp_path / "purge.db", retention_hours=1)
    now = 1_000_000_000_000
    store.record(_sample(cid="a", ts=now - 4_000_000))  # > 1 h old
    store.record(_sample(cid="a", ts=now - 1_000_000))  # < 1 h old
    try:
        removed = store.purge(now_ms=now)
        assert removed == 1
        assert [p["ts"] for p in store.query("a", 0)] == [now - 1_000_000]
    finally:
        store.close()


def test_store_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKY_STATS_RETENTION_HOURS", "48")
    monkeypatch.setenv("DOCKY_STATS_DOWNSAMPLE_SECONDS", "60")
    store = StatsStore(db_path=tmp_path / "env.db")
    try:
        assert store.retention_hours == 48.0
        assert store.downsample_seconds == 60.0
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Watch set (resolve, idempotence, TTL)
# ---------------------------------------------------------------------------

def test_watch_resolves_and_reports_canonical_id(make_manager):
    container = FakeStatsContainer(stream_factory=_empty_gen)
    manager = make_manager([container])
    try:
        assert manager.watch(["web"]) == ["abcdef123456"]
        assert manager.watched() == ["abcdef123456"]
        assert manager.watch_info()[0]["name"] == "web"
    finally:
        manager.stop()


def test_watch_is_idempotent_and_refreshes_ttl(make_manager):
    container = FakeStatsContainer(stream_factory=_empty_gen)
    manager = make_manager([container], ttl_seconds=30)
    try:
        manager.watch(["web"])
        entry = manager._watched["abcdef123456"]
        streamer = entry.streamer
        entry.last_seen -= 100  # make it stale
        stale = entry.last_seen
        assert manager.watch(["abcdef123456"]) == ["abcdef123456"]
        assert manager._watched["abcdef123456"].streamer is streamer
        assert manager._watched["abcdef123456"].last_seen > stale
        assert manager.sweep() == []  # refreshed -> not expired
    finally:
        manager.stop()


def test_watch_dedupes_name_and_id(make_manager):
    container = FakeStatsContainer(stream_factory=_empty_gen)
    manager = make_manager([container])
    try:
        manager.watch(["web"])
        assert manager.watch(["abcdef123456"]) == ["abcdef123456"]
        assert manager.watched() == ["abcdef123456"]
    finally:
        manager.stop()


def test_watch_unknown_is_skipped(make_manager):
    manager = make_manager([])
    assert manager.watch(["ghost"]) == []
    assert manager.watched() == []


def test_unwatch_by_name_and_id(make_manager):
    container = FakeStatsContainer(stream_factory=_empty_gen)
    manager = make_manager([container])
    try:
        manager.watch(["web"])
        streamer = manager._watched["abcdef123456"].streamer
        removed = manager.unwatch(["web"])
        assert removed == ["abcdef123456"]
        assert manager.watched() == []
        assert not streamer.is_alive()
        assert manager.unwatch(["abcdef123456"]) == []
    finally:
        manager.stop()


def test_sweep_expires_stale_entry(make_manager):
    container = FakeStatsContainer(stream_factory=_empty_gen)
    manager = make_manager([container], ttl_seconds=0.01)
    try:
        manager.watch(["web"])
        streamer = manager._watched["abcdef123456"].streamer
        time.sleep(0.03)
        assert manager.sweep() == ["abcdef123456"]
        assert manager.watched() == []
        assert not streamer.is_alive()
    finally:
        manager.stop()


def test_sweep_revives_dead_streamer(make_manager):
    container = FakeStatsContainer(stream_factory=_empty_gen)
    manager = make_manager([container], ttl_seconds=30)
    try:
        manager.watch(["web"])
        entry = manager._watched["abcdef123456"]
        old = entry.streamer
        old.stop()
        assert not old.is_alive()
        assert manager.sweep() == []
        assert entry.streamer is not old
        assert entry.streamer.is_alive()
    finally:
        manager.stop()


def test_resolve_container_prefers_watch_registry(make_manager):
    container = FakeStatsContainer(stream_factory=_empty_gen)
    manager = make_manager([container])
    try:
        manager.watch(["web"])
        assert manager.resolve_container("web") == ("abcdef123456", "web")
        assert manager.resolve_container("abcdef123456") == ("abcdef123456", "web")
    finally:
        manager.stop()


def test_resolve_container_unknown_returns_none(make_manager):
    manager = make_manager([])
    assert manager.resolve_container("ghost") is None


# ---------------------------------------------------------------------------
# Streamer (start/stop, ring buffer, stop closes threads)
# ---------------------------------------------------------------------------

def test_streamer_feeds_ring_buffer(make_manager):
    container = FakeStatsContainer(stream_factory=_infinite_gen())
    manager = make_manager([container])
    try:
        manager.watch(["web"])
        entry = manager._watched["abcdef123456"]
        assert _wait_until(lambda: len(entry.ring) >= 3)
        assert "state" in entry.ring[-1]
        assert entry.last_sample is not None
    finally:
        manager.stop()


def test_ring_buffer_caps_size_and_keeps_order(make_manager):
    manager = make_manager([], ring_size=3)
    entry = _inject_entry(manager, "abcdef123456", "web")
    for ts in range(5):
        manager._handle_sample(_sample(ts=ts))
    assert [s["ts"] for s in entry.ring] == [2, 3, 4]
    assert entry.ring.maxlen == 3


def test_stop_closes_all_streamer_threads(make_manager):
    container = FakeStatsContainer(stream_factory=_infinite_gen())
    manager = make_manager([container])
    manager.watch(["web"])
    streamers = [entry.streamer for entry in manager._watched.values()]
    assert streamers and all(s.is_alive() for s in streamers)
    manager.stop()
    assert manager.watched() == []
    assert all(not s.is_alive() for s in streamers)


def test_handle_sample_ignored_when_not_watched(make_manager):
    manager = make_manager([])
    before = threading.active_count()
    manager._handle_sample(_sample(ts=1))  # no entry -> no ring, no store write
    assert manager.store.count("abcdef123456") == 0
    assert threading.active_count() == before


def test_streamer_handles_container_disappearing(make_manager):
    # get_docker_client raises -> streamer retries and stays alive, no crash.
    manager = make_manager([])

    def _boom():
        raise docker.errors.DockerException("daemon down")

    import agent.stats_stream as ss
    original = dm.get_docker_client
    dm.get_docker_client = _boom
    try:
        entry = _inject_entry(manager, "abcdef123456", "web")
        entry.streamer = manager._spawn_streamer(entry)
        time.sleep(0.05)
        assert entry.streamer.is_alive()
    finally:
        dm.get_docker_client = original
        manager.stop()


# ---------------------------------------------------------------------------
# Broadcast (snapshot + live + clean unsubscribe)
# ---------------------------------------------------------------------------

async def test_broadcast_snapshot_and_live_then_unsubscribe(make_manager):
    manager = make_manager([])
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=2)
    token = manager.add_subscriber(loop, queue)

    _inject_entry(manager, "abcdef123456", "web", last_sample=_sample(ts=1))
    assert [s["ts"] for s in manager.snapshot()] == [1]

    manager._handle_sample(_sample(ts=2))
    live = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert live["ts"] == 2

    assert manager.remove_subscriber(token) is True
    manager._handle_sample(_sample(ts=3))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(queue.get(), timeout=0.1)


async def test_broadcast_drops_oldest_when_subscriber_lags(make_manager):
    manager = make_manager([])
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    manager.add_subscriber(loop, queue)
    _inject_entry(manager, "abcdef123456", "web")

    manager._handle_sample(_sample(ts=1))
    manager._handle_sample(_sample(ts=2))
    got = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert got["ts"] == 2  # oldest dropped, latest kept


# ---------------------------------------------------------------------------
# History (merged ring + persisted)
# ---------------------------------------------------------------------------

def test_get_history_merges_ring_and_persisted(make_manager):
    manager = make_manager([])
    now = int(time.time() * 1000)
    _inject_entry(
        manager, "abcdef123456", "web",
        ring=[_sample(ts=now - 1_000, cpu_percent=2.0),
              _sample(ts=now - 500, cpu_percent=3.0)],
    )
    manager.store.record(_sample(ts=now - 100_000, cpu_percent=1.0))
    manager.store.record(_sample(ts=now - 1_000, cpu_percent=1.5))  # ring overrides
    manager.store.record(_sample(ts=now - 2_000_000, cpu_percent=9.0))  # out of 900 s

    points = manager.get_history("web", 900)

    timestamps = [p["ts"] for p in points]
    assert timestamps == sorted(timestamps)
    assert now - 2_000_000 not in timestamps
    assert set(points[0]) == {
        "ts", "cpu_percent", "mem_usage", "mem_limit",
        "mem_percent", "net_rx", "net_tx",
    }
    overridden = next(p for p in points if p["ts"] == now - 1_000)
    assert overridden["cpu_percent"] == 2.0


def test_get_history_supported_windows(make_manager):
    manager = make_manager([])
    now = int(time.time() * 1000)
    _inject_entry(manager, "abcdef123456", "web", ring=[_sample(ts=now - 1000)])
    for window in (900, 3600, 86400):
        assert manager.get_history("web", window)


def test_get_history_rejects_unsupported_window(make_manager):
    manager = make_manager([])
    with pytest.raises(ValueError):
        manager.get_history("web", 42)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def test_stats_watch_requires_auth(agent_client):
    resp = agent_client.post("/agent/stats/watch", json={"containers": ["web"]})
    assert resp.status_code == 401


def test_stats_unwatch_and_list_require_auth(agent_client):
    assert agent_client.post("/agent/stats/unwatch", json={"containers": ["web"]}).status_code == 401
    assert agent_client.get("/agent/stats/watch").status_code == 401


def test_stats_watch_route(agent_client, api_key_header, monkeypatch):
    captured = {}

    def _watch(containers):
        captured["containers"] = containers
        return ["abcdef123456"]

    monkeypatch.setattr(stats_stream, "watch", _watch)

    resp = agent_client.post(
        "/agent/stats/watch", headers=api_key_header, json={"containers": ["web"]}
    )

    assert resp.status_code == 200
    assert resp.json() == {"success": True, "watched": ["abcdef123456"]}
    assert captured["containers"] == ["web"]


def test_stats_watch_route_accepts_ids_alias(agent_client, api_key_header, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        stats_stream, "watch", lambda containers: captured.setdefault("c", containers) or []
    )
    resp = agent_client.post(
        "/agent/stats/watch", headers=api_key_header, json={"ids": ["web"]}
    )
    assert resp.status_code == 200
    assert captured["c"] == ["web"]


@pytest.mark.parametrize("payload", [{"containers": "nope"}, {"containers": [1]}, [], {"containers": None}])
def test_stats_watch_invalid_body_400(agent_client, api_key_header, payload):
    resp = agent_client.post("/agent/stats/watch", headers=api_key_header, json=payload)
    assert resp.status_code == 400


def test_stats_unwatch_route(agent_client, api_key_header, monkeypatch):
    monkeypatch.setattr(stats_stream, "unwatch", lambda containers: ["abcdef123456"])
    monkeypatch.setattr(stats_stream, "watched", lambda: [])

    resp = agent_client.post(
        "/agent/stats/unwatch", headers=api_key_header, json={"containers": ["web"]}
    )

    assert resp.status_code == 200
    assert resp.json() == {"success": True, "unwatched": ["abcdef123456"], "watched": []}


def test_stats_watch_list_route(agent_client, api_key_header, monkeypatch):
    monkeypatch.setattr(stats_stream, "watched", lambda: ["abcdef123456"])
    resp = agent_client.get("/agent/stats/watch", headers=api_key_header)
    assert resp.status_code == 200
    assert resp.json() == {"watched": ["abcdef123456"]}


def test_history_requires_auth(agent_client):
    resp = agent_client.get("/agent/containers/web/stats/history?window=900")
    assert resp.status_code == 401


def test_history_unsupported_window_400(agent_client, api_key_header):
    resp = agent_client.get(
        "/agent/containers/web/stats/history?window=42", headers=api_key_header
    )
    assert resp.status_code == 400


def test_history_unknown_container_404(agent_client, api_key_header, monkeypatch):
    monkeypatch.setattr(stats_stream, "resolve_container", lambda ref: None)
    resp = agent_client.get(
        "/agent/containers/ghost/stats/history?window=900", headers=api_key_header
    )
    assert resp.status_code == 404


def test_history_route_ok(agent_client, api_key_header, monkeypatch):
    monkeypatch.setattr(
        stats_stream, "resolve_container", lambda ref: ("abcdef123456", "web")
    )
    calls = {}

    def _history(cid, window):
        calls["args"] = (cid, window)
        return [{"ts": 1, "cpu_percent": 1.0, "mem_usage": 2, "mem_limit": 3,
                 "mem_percent": 4.0, "net_rx": 5, "net_tx": 6}]

    monkeypatch.setattr(stats_stream, "get_history", _history)

    resp = agent_client.get(
        "/agent/containers/web/stats/history?window=3600", headers=api_key_header
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["container"] == "web"
    assert body["window"] == 3600
    assert body["points"][0]["ts"] == 1
    assert calls["args"] == ("abcdef123456", 3600)


def test_stats_stream_ws_requires_auth(agent_client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with agent_client.websocket_connect("/agent/stats/stream"):
            pass


def test_stats_stream_ws_snapshot_and_live(agent_client, api_key_header, monkeypatch):
    captured = {}
    removed = []

    def _add(loop, queue):
        captured["loop"] = loop
        captured["queue"] = queue
        return 7

    monkeypatch.setattr(stats_stream, "add_subscriber", _add)
    monkeypatch.setattr(
        stats_stream, "remove_subscriber", lambda token: removed.append(token) or True
    )
    monkeypatch.setattr(stats_stream, "snapshot", lambda: [_sample(ts=1)])

    with agent_client.websocket_connect("/agent/stats/stream?api_key=test-key") as ws:
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"
        assert snapshot["samples"][0]["ts"] == 1

        captured["loop"].call_soon_threadsafe(
            captured["queue"].put_nowait, _sample(ts=2)
        )
        live = ws.receive_json()
        assert live["type"] == "sample"
        assert live["sample"]["ts"] == 2

    assert removed == [7]
