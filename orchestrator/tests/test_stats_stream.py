"""Tests for the orchestrator real-time stats stream manager (LOT 2).

Covers the hot set (per-source refcounts + Homy TTL), the one-WS-per-agent
runner (open/close, periodic re-watch, reconnect backoff), the sample dispatch
to frontend subscribers, and the hot-cache read path used by the integration
façade. Everything runs on a fake ``AgentManager``/``websockets.connect`` — no
real network, no real Docker.
"""

import asyncio
import contextlib
import json
import time

import pytest

from app.agent_manager import stats_stream as ss_mod
from app.agent_manager.stats_stream import (
    AgentStatsStreamManager,
    SOURCE_HOMY,
    SOURCE_UI,
    _Watch,
)


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------

class FakeAgentManager:
    """Stand-in for the ``AgentManager`` singleton (metadata + REST seam)."""

    def __init__(self, agents=None):
        self.agents = dict(agents or {})
        self.requests = []

    async def _request(self, agent, method, path, **kwargs):
        self.requests.append(
            {"agent": agent, "method": method, "path": path, "kwargs": kwargs}
        )
        return {"success": True}

    def _agent_ws_connect_kwargs(self, info):
        return {}

    def watch_calls(self, agent=None):
        return [
            r
            for r in self.requests
            if r["path"] == "/agent/stats/watch"
            and (agent is None or r["agent"] == agent)
        ]

    def unwatch_calls(self, agent=None):
        return [
            r
            for r in self.requests
            if r["path"] == "/agent/stats/unwatch"
            and (agent is None or r["agent"] == agent)
        ]


class FakeWS:
    """Async-context-manager fake for a ``websockets`` connection."""

    def __init__(self, messages=None):
        self.messages = list(messages or [])
        self.closed = False
        self.entered = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.messages:
            return self.messages.pop(0)
        # Keep the connection "open" without busy-looping.
        await asyncio.sleep(3600)
        raise StopAsyncIteration


def _sample(cid="c1", name="web", ts=1_700_000_000_000, **overrides):
    sample = {
        "ts": ts,
        "id": cid,
        "name": name,
        "state": "running",
        "cpu_percent": 42.1,
        "cpu_count": 4,
        "mem_usage": 100,
        "mem_limit": 1000,
        "mem_percent": 10.0,
        "mem_cache": 5,
        "net_rx": 7,
        "net_tx": 8,
    }
    sample.update(overrides)
    return sample


async def _wait_for(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0)
    return bool(predicate())


def _fake_ws_connect(calls, ws=None):
    ws = ws or FakeWS()

    def connect(url, **kwargs):
        calls.append(url)
        return ws

    return connect


# ---------------------------------------------------------------------------
# Hot set — refcounts and sources
# ---------------------------------------------------------------------------

async def test_subscribe_refcount_and_single_ws_per_agent():
    fake = FakeAgentManager({"A": {"url": "http://a:8080"}})
    connects = []
    ws = FakeWS()
    mgr = AgentStatsStreamManager(
        manager=fake, connect=_fake_ws_connect(connects, ws), backoff_min=0.01
    )
    try:
        await mgr.subscribe("A", "c1", SOURCE_UI)
        await mgr.subscribe("A", "c2", SOURCE_UI)
        await mgr.subscribe("A", "c1", SOURCE_UI)  # refcount 2

        assert await _wait_for(lambda: len(connects) == 1)
        assert connects == ["ws://a:8080/agent/stats/stream"]
        assert mgr.watched_containers("A") == ["c1", "c2"]

        assert await _wait_for(lambda: bool(fake.watch_calls("A")))
        watched = set(fake.watch_calls("A")[-1]["kwargs"]["json"]["containers"])
        assert watched == {"c1", "c2"}

        # First release of c1 keeps it (refcount 2 -> 1).
        await mgr.unsubscribe("A", "c1", SOURCE_UI)
        assert mgr.watched_containers("A") == ["c1", "c2"]
        # Second release removes c1 and sends an explicit unwatch.
        await mgr.unsubscribe("A", "c1", SOURCE_UI)
        assert mgr.watched_containers("A") == ["c2"]
        assert await _wait_for(lambda: bool(fake.unwatch_calls("A")))
        assert fake.unwatch_calls("A")[-1]["kwargs"]["json"]["containers"] == ["c1"]

        # Removing the last watch closes the agent WS.
        await mgr.unsubscribe("A", "c2", SOURCE_UI)
        assert mgr.watched_containers("A") == []
        assert await _wait_for(lambda: ws.closed)
        assert ws.closed
    finally:
        mgr.stop()


async def test_two_agents_open_two_ws():
    fake = FakeAgentManager(
        {
            "A": {"url": "http://a:8080"},
            "B": {"url": "http://b:8080"},
        }
    )
    connects = []
    mgr = AgentStatsStreamManager(
        manager=fake, connect=_fake_ws_connect(connects), backoff_min=0.01
    )
    try:
        await mgr.subscribe("A", "c1", SOURCE_UI)
        await mgr.subscribe("B", "c1", SOURCE_UI)
        assert await _wait_for(lambda: len(connects) == 2)
        assert sorted(connects) == [
            "ws://a:8080/agent/stats/stream",
            "ws://b:8080/agent/stats/stream",
        ]
    finally:
        mgr.stop()


async def test_homy_ttl_expiry_and_refresh():
    clock = {"t": 1000.0}
    fake = FakeAgentManager({"A": {"url": "http://a:8080"}})
    mgr = AgentStatsStreamManager(
        manager=fake, clock=lambda: clock["t"], connect=lambda *a, **k: FakeWS()
    )
    try:
        await mgr.mark_hot_set("A", ["c1"])
        assert mgr.watched_containers("A") == ["c1"]

        clock["t"] += 30
        mgr.sweep()
        assert mgr.watched_containers("A") == ["c1"]

        # A fresh ``touch`` re-arms the 60 s TTL.
        await mgr.mark_hot_set("A", ["c1"])
        clock["t"] += 59
        mgr.sweep()
        assert mgr.watched_containers("A") == ["c1"]

        clock["t"] += 2
        assert mgr.sweep() == ["A"]
        assert mgr.watched_containers("A") == []
    finally:
        mgr.stop()


async def test_ui_survives_homy_expiry_then_release():
    clock = {"t": 0.0}
    fake = FakeAgentManager({"A": {"url": "http://a:8080"}})
    mgr = AgentStatsStreamManager(
        manager=fake, clock=lambda: clock["t"], connect=lambda *a, **k: FakeWS()
    )
    try:
        await mgr.subscribe("A", "c1", SOURCE_UI)
        await mgr.mark_hot_set("A", ["c1"])

        clock["t"] = 61.0
        mgr.sweep()
        # Homy expired but the UI source keeps the entry alive.
        assert mgr.watched_containers("A") == ["c1"]
        state = mgr._agents["A"]
        assert state.watches["c1"].counts == {SOURCE_UI: 1}

        await mgr.unsubscribe("A", "c1", SOURCE_UI)
        assert mgr.watched_containers("A") == []
    finally:
        mgr.stop()


async def test_mark_hot_set_is_idempotent_no_refcount_leak():
    fake = FakeAgentManager({"A": {"url": "http://a:8080"}})
    mgr = AgentStatsStreamManager(manager=fake, connect=lambda *a, **k: FakeWS())
    try:
        for _ in range(5):
            await mgr.mark_hot_set("A", ["c1"])
        state = mgr._agents["A"]
        assert state.watches["c1"].counts == {SOURCE_HOMY: 1}
    finally:
        mgr.stop()


# ---------------------------------------------------------------------------
# Runner — re-watch + reconnect backoff
# ---------------------------------------------------------------------------

async def test_rewatch_loop_posts_periodically():
    fake = FakeAgentManager({"A": {"url": "http://a:8080"}})
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        await asyncio.sleep(0)

    mgr = AgentStatsStreamManager(
        manager=fake, connect=lambda *a, **k: FakeWS(), sleep=sleep, rewatch_interval=20.0
    )
    state = mgr._agent_state("A")
    state.connected = True
    state.watches["c1"] = _Watch("c1", {SOURCE_UI: 1})

    task = asyncio.ensure_future(mgr._rewatch_loop("A"))
    try:
        assert await _wait_for(lambda: bool(fake.watch_calls("A")))
        assert 20.0 in sleeps
        assert fake.watch_calls("A")[-1]["kwargs"]["json"]["containers"] == ["c1"]
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_reconnect_uses_exponential_backoff():
    fake = FakeAgentManager({"A": {"url": "http://a:8080"}})

    class FailingWS:
        async def __aenter__(self):
            raise ConnectionError("unreachable")

        async def __aexit__(self, *exc):
            return False

    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        await asyncio.sleep(0)

    mgr = AgentStatsStreamManager(
        manager=fake,
        connect=lambda *a, **k: FailingWS(),
        sleep=sleep,
        backoff_min=1.0,
        backoff_max=4.0,
    )
    try:
        await mgr.subscribe("A", "c1", SOURCE_UI)
        assert await _wait_for(
            lambda: len([value for value in sleeps if value <= 4.0]) >= 4
        )
        backoffs = [value for value in sleeps if value <= 4.0]
        assert backoffs[:4] == [1.0, 2.0, 4.0, 4.0]
    finally:
        mgr.stop()
        await asyncio.sleep(0)


async def test_runner_ingests_snapshot_and_ignores_garbage():
    fake = FakeAgentManager({"A": {"url": "http://a:8080"}})
    snap = _sample(cid="c1", name="web", ts=1000)
    ws = FakeWS(
        [
            json.dumps({"type": "snapshot", "samples": [snap]}),
            "not-json",
            json.dumps({"type": "unknown-type", "x": 1}),
        ]
    )
    mgr = AgentStatsStreamManager(manager=fake, connect=lambda *a, **k: ws)
    queue: asyncio.Queue = asyncio.Queue()
    try:
        await mgr.subscribe("A", "c1", SOURCE_UI)
        assert await _wait_for(
            lambda: bool(mgr.get_samples_for_targets([("A", "c1")]))
        )
        assert mgr.get_samples_for_targets([("A", "c1")])[0]["ts"] == 1000
        # A snapshot is not a live sample: no broadcast.
        assert queue.empty()
    finally:
        mgr.stop()


async def test_runner_dispatches_live_sample():
    fake = FakeAgentManager({"A": {"url": "http://a:8080"}})
    live = _sample(cid="c1", name="web", ts=2000)
    ws = FakeWS([json.dumps({"type": "sample", "sample": live})])
    mgr = AgentStatsStreamManager(manager=fake, connect=lambda *a, **k: ws)
    queue: asyncio.Queue = asyncio.Queue()
    token = mgr.add_subscriber(asyncio.get_running_loop(), queue)
    try:
        await mgr.subscribe("A", "c1", SOURCE_UI)
        await mgr.client_subscribe(token, [("A", "c1")])
        message = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert message["type"] == "sample"
        assert message["sample"]["ts"] == 2000
    finally:
        mgr.stop()


# ---------------------------------------------------------------------------
# Frontend subscribers
# ---------------------------------------------------------------------------

async def test_client_subscribe_snapshot_and_unsubscribe_stops_dispatch():
    fake = FakeAgentManager({"A": {"url": "http://a:8080"}})
    mgr = AgentStatsStreamManager(manager=fake, connect=lambda *a, **k: FakeWS())
    queue: asyncio.Queue = asyncio.Queue()
    token = mgr.add_subscriber(asyncio.get_running_loop(), queue)
    try:
        added = await mgr.client_subscribe(token, [("A", "c1")])
        assert added == [("A", "c1")]
        assert mgr.watched_containers("A") == ["c1"]

        sample = _sample(cid="c1", name="web")
        mgr.ingest_sample("A", sample)
        message = queue.get_nowait()
        assert message == {"type": "sample", "sample": sample}

        assert mgr.get_samples_for_targets([("A", "c1")]) == [sample]

        await mgr.client_unsubscribe(token, [("A", "c1")])
        assert mgr.watched_containers("A") == []
        mgr.ingest_sample("A", sample)
        assert queue.empty()
    finally:
        mgr.stop()


async def test_remove_subscriber_releases_ui_targets():
    fake = FakeAgentManager({"A": {"url": "http://a:8080"}})
    mgr = AgentStatsStreamManager(manager=fake, connect=lambda *a, **k: FakeWS())
    queue: asyncio.Queue = asyncio.Queue()
    token = mgr.add_subscriber(asyncio.get_running_loop(), queue)
    try:
        await mgr.client_subscribe(token, [("A", "c1"), ("A", "c2")])
        assert set(mgr.watched_containers("A")) == {"c1", "c2"}
        mgr.remove_subscriber(token)
        assert await _wait_for(lambda: mgr.watched_containers("A") == [])
        assert mgr.watched_containers("A") == []
    finally:
        mgr.stop()


async def test_broadcast_only_reaches_matching_subscribers():
    fake = FakeAgentManager({"A": {"url": "http://a:8080"}})
    mgr = AgentStatsStreamManager(manager=fake, connect=lambda *a, **k: FakeWS())
    q1: asyncio.Queue = asyncio.Queue()
    q2: asyncio.Queue = asyncio.Queue()
    t1 = mgr.add_subscriber(asyncio.get_running_loop(), q1)
    t2 = mgr.add_subscriber(asyncio.get_running_loop(), q2)
    try:
        await mgr.client_subscribe(t1, [("A", "web")])
        await mgr.client_subscribe(t2, [("A", "other")])
        mgr.ingest_sample("A", _sample(cid="abcdef123456", name="web"))
        assert q1.qsize() == 1
        assert q2.empty()
    finally:
        mgr.stop()


# ---------------------------------------------------------------------------
# Hot cache reads
# ---------------------------------------------------------------------------

async def test_get_fresh_sample_fresh_vs_stale_and_name_lookup():
    clock = {"t": 1000.0}
    mgr = AgentStatsStreamManager(
        manager=FakeAgentManager(), wall_clock=lambda: clock["t"]
    )
    sample = _sample(cid="abcdef123456", name="web", ts=1_000_000)
    mgr.ingest_sample("A", sample)

    assert mgr.get_fresh_sample("A", "abcdef123456") is sample
    assert mgr.get_fresh_sample("A", "web") is sample
    assert mgr.get_fresh_sample("A", "abcdef") is sample  # id prefix

    clock["t"] = 1003.0
    assert mgr.get_fresh_sample("A", "abcdef123456") is None
    assert mgr.get_fresh_sample("A", "ghost") is None


async def test_get_samples_for_targets_deduplicates():
    mgr = AgentStatsStreamManager(manager=FakeAgentManager())
    sample = _sample(cid="c1", name="web")
    mgr.ingest_sample("A", sample)
    samples = mgr.get_samples_for_targets([("A", "c1"), ("A", "web"), ("B", "c1")])
    assert samples == [sample]


# ---------------------------------------------------------------------------
# Message parsing robustness
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        b"not json",
        json.dumps([1, 2, 3]),
        json.dumps({"type": "weird"}),
        json.dumps({"type": "sample"}),
        json.dumps({"type": "snapshot", "samples": "nope"}),
        None,
    ],
)
async def test_handle_message_never_raises(raw):
    mgr = AgentStatsStreamManager(manager=FakeAgentManager())
    if raw is None:
        mgr._handle_message("A", None)
    else:
        mgr._handle_message("A", raw)
    assert mgr.watched_containers("A") == []


async def test_handle_message_ignores_unknown_and_ingests_known():
    mgr = AgentStatsStreamManager(manager=FakeAgentManager())
    mgr._handle_message("A", json.dumps({"type": "snapshot", "samples": [_sample(ts=5)]}))
    assert mgr.get_samples_for_targets([("A", "c1")])[0]["ts"] == 5
    mgr._handle_message("A", json.dumps({"type": "whatever"}))
    assert mgr.get_samples_for_targets([("A", "c1")])[0]["ts"] == 5


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------

async def test_stop_is_idempotent_and_clears_state():
    fake = FakeAgentManager({"A": {"url": "http://a:8080"}})
    mgr = AgentStatsStreamManager(manager=fake, connect=lambda *a, **k: FakeWS())
    await mgr.subscribe("A", "c1", SOURCE_UI)
    mgr.stop()
    mgr.stop()
    assert mgr.watched_containers("A") == []
    assert mgr._subscribers == {}


async def test_module_facade_wrappers():
    fake = FakeAgentManager({"A": {"url": "http://a:8080"}})
    mgr = AgentStatsStreamManager(manager=fake, connect=lambda *a, **k: FakeWS())
    previous = ss_mod.get_manager()
    ss_mod.set_manager(mgr)
    try:
        await ss_mod.mark_hot_set("A", ["c1"])
        assert mgr.watched_containers("A") == ["c1"]
        await mgr.unsubscribe("A", "c1", SOURCE_HOMY)
        assert mgr.watched_containers("A") == []
    finally:
        mgr.stop()
        ss_mod.set_manager(previous)
