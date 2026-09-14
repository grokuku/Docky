"""Route-level tests for the orchestrator stats streaming (LOT 2).

Covers:

- ``GET /api/containers/{id}/stats/history`` (JWT proxy: 200/400/404/502);
- ``WS /api/stats/stream`` (JWT cookie auth, subscribe/snapshot/live/unsubscribe
  protocol, malformed message handling, disconnect cleanup);
- ``GET /api/integration/v1/.../stats/history`` (Bearer, ``{error, code}``);
- the integration façade hot-set behaviour (unit + batch, fresh vs live).

Everything is hermetic: the agent manager is mocked and the stats stream
manager is replaced by a tiny in-memory fake.
"""

import httpx
import pytest

from app.routes import containers as containers_mod
from app.routes import integration as integration_mod


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def integration_key(orchestrator_client):
    from app.routes.integration import get_integration_api_key

    return get_integration_api_key()


@pytest.fixture
def bearer(integration_key):
    return {"Authorization": f"Bearer {integration_key}"}


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://agent:8080/x")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("agent error", request=request, response=response)


def _online(manager, name="A", status="online"):
    manager.agents = {
        name: {"url": f"http://{name.lower()}:8080", "status": status, "last_check": 0}
    }
    return manager


def _sample(ts=1_700_000_000_000, cid="c1", name="web"):
    """A streamed sample (trimmed shape, raw multi-core CPU)."""
    return {
        "ts": ts,
        "id": cid,
        "name": name,
        "state": "running",
        "cpu_percent": 400.0,
        "cpu_count": 4,
        "mem_usage": 600,
        "mem_limit": 10000,
        "mem_percent": 6.0,
        "mem_cache": 400,
        "net_rx": 1234,
        "net_tx": 5678,
    }


class WSFakeStatsManager:
    """Minimal fake for the frontend ``WS /api/stats/stream`` route."""

    def __init__(self, samples=None):
        self.samples = dict(samples or {})
        self.queues = {}
        self.loops = {}
        self.subscribed = []
        self.unsubscribed = []
        self.removed = []

    def add_subscriber(self, loop, queue):
        token = len(self.queues) + 1
        self.queues[token] = queue
        self.loops[token] = loop
        return token

    def remove_subscriber(self, token):
        self.removed.append(token)
        self.queues.pop(token, None)

    async def client_subscribe(self, token, targets):
        added = [target for target in targets if target not in self.subscribed]
        self.subscribed.extend(added)
        return added

    async def client_unsubscribe(self, token, targets):
        self.unsubscribed.extend(targets)

    def get_samples_for_targets(self, targets):
        return [self.samples[target] for target in targets if target in self.samples]


class HotFakeStatsManager:
    """Fake hot-set manager for the integration façade cache tests."""

    def __init__(self, samples=None):
        self.samples = dict(samples or {})
        self.hot_calls = []

    async def mark_hot_set(self, agent, containers):
        self.hot_calls.append((agent, list(containers)))

    def get_fresh_sample(self, agent, container, max_age=None):
        return self.samples.get((agent, container))


# ---------------------------------------------------------------------------
# UI history proxy
# ---------------------------------------------------------------------------

def test_ui_history_requires_auth(orchestrator_client):
    resp = orchestrator_client.get(
        "/api/containers/c1/stats/history?agent=A&window=900"
    )
    assert resp.status_code == 401


def test_ui_history_unsupported_window(auth_client, mock_agent_manager):
    resp = auth_client.get(
        "/api/containers/c1/stats/history?agent=Test Agent&window=123"
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["supported"] == [900, 3600, 86400]
    mock_agent_manager._request.assert_not_awaited()


def test_ui_history_ok_proxies_agent(auth_client, mock_agent_manager):
    mock_agent_manager._request.return_value = {
        "container": "c1",
        "window": 3600,
        "points": [{"ts": 1, "cpu_percent": 2.0}],
    }
    resp = auth_client.get(
        "/api/containers/c1/stats/history?agent=Test Agent&window=3600"
    )
    assert resp.status_code == 200
    assert resp.json()["points"] == [{"ts": 1, "cpu_percent": 2.0}]
    args, kwargs = mock_agent_manager._request.await_args
    assert args[0] == "Test Agent"
    assert args[1] == "GET"
    assert args[2].endswith("/agent/containers/c1/stats/history")
    assert kwargs["params"] == {"window": 3600}


def test_ui_history_not_found_404(auth_client, mock_agent_manager):
    mock_agent_manager._request.side_effect = _http_status_error(404)
    resp = auth_client.get(
        "/api/containers/missing/stats/history?agent=Test Agent&window=900"
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Container not found"


def test_ui_history_agent_400(auth_client, mock_agent_manager):
    mock_agent_manager._request.side_effect = _http_status_error(400)
    resp = auth_client.get(
        "/api/containers/c1/stats/history?agent=Test Agent&window=900"
    )
    assert resp.status_code == 400


def test_ui_history_unreachable_502(auth_client, mock_agent_manager):
    mock_agent_manager._request.side_effect = RuntimeError("connection refused")
    resp = auth_client.get(
        "/api/containers/c1/stats/history?agent=Test Agent&window=900"
    )
    assert resp.status_code == 502
    assert "communicate" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Frontend WebSocket /api/stats/stream
# ---------------------------------------------------------------------------

def test_ws_stats_stream_requires_auth(orchestrator_client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with orchestrator_client.websocket_connect("/api/stats/stream"):
            pass


def test_ws_stats_stream_subscribe_snapshot_live_and_unsubscribe(
    auth_client, monkeypatch
):
    fake = WSFakeStatsManager(samples={("A", "c1"): {"ts": 1, "id": "c1"}})
    monkeypatch.setattr(containers_mod, "_stats_manager", lambda: fake)

    with auth_client.websocket_connect("/api/stats/stream") as ws:
        ws.send_json(
            {"type": "subscribe", "targets": [{"agent": "A", "container": "c1"}]}
        )
        snapshot = ws.receive_json()
        assert snapshot == {"type": "snapshot", "samples": [{"ts": 1, "id": "c1"}]}

        # Simulate a live sample arriving from an agent runner.
        loop = fake.loops[1]
        queue = fake.queues[1]
        loop.call_soon_threadsafe(
            queue.put_nowait, {"type": "sample", "sample": {"ts": 2, "id": "c1"}}
        )
        live = ws.receive_json()
        assert live == {"type": "sample", "sample": {"ts": 2, "id": "c1"}}

        # Unknown types are ignored; unsubscribe is acknowledged only by state.
        ws.send_json({"type": "unknown-type"})
        ws.send_json(
            {"type": "unsubscribe", "targets": [{"agent": "A", "container": "c1"}]}
        )

    assert fake.subscribed == [("A", "c1")]
    assert fake.unsubscribed == [("A", "c1")]
    assert fake.removed == [1]


def test_ws_stats_stream_malformed_message_returns_error(auth_client, monkeypatch):
    fake = WSFakeStatsManager()
    monkeypatch.setattr(containers_mod, "_stats_manager", lambda: fake)

    with auth_client.websocket_connect("/api/stats/stream") as ws:
        ws.send_text("{not json")
        message = ws.receive_json()
        assert message["type"] == "error"


def test_ws_stats_stream_ignores_bad_targets(auth_client, monkeypatch):
    fake = WSFakeStatsManager()
    monkeypatch.setattr(containers_mod, "_stats_manager", lambda: fake)

    with auth_client.websocket_connect("/api/stats/stream") as ws:
        ws.send_json(
            {
                "type": "subscribe",
                "targets": [
                    {"agent": "A", "container": "c1"},
                    {"agent": "", "container": "c2"},
                    "not-a-dict",
                    {"agent": "A"},
                ],
            }
        )
        snapshot = ws.receive_json()
        assert snapshot["samples"] == []

    assert fake.subscribed == [("A", "c1")]


# ---------------------------------------------------------------------------
# Integration history endpoint
# ---------------------------------------------------------------------------

def test_integration_history_requires_bearer(orchestrator_client, integration_key):
    resp = orchestrator_client.get(
        "/api/integration/v1/agents/A/containers/c1/stats/history?window=900"
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"


def test_integration_history_invalid_window(
    orchestrator_client, mock_agent_manager, bearer
):
    _online(mock_agent_manager)
    resp = orchestrator_client.get(
        "/api/integration/v1/agents/A/containers/c1/stats/history?window=123",
        headers=bearer,
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_request"


def test_integration_history_ok(orchestrator_client, mock_agent_manager, bearer):
    _online(mock_agent_manager)
    mock_agent_manager._request.return_value = {
        "container": "c1",
        "window": 3600,
        "points": [{"ts": 1, "cpu_percent": 2.0, "net_rx": 3, "net_tx": 4}],
    }
    resp = orchestrator_client.get(
        "/api/integration/v1/agents/A/containers/c1/stats/history?window=3600",
        headers=bearer,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["window"] == 3600
    assert body["points"][0]["net_rx"] == 3
    args, kwargs = mock_agent_manager._request.await_args
    assert args[0] == "A"
    assert args[2].endswith("/agent/containers/c1/stats/history")
    assert kwargs["params"] == {"window": 3600}


def test_integration_history_not_found_404(
    orchestrator_client, mock_agent_manager, bearer
):
    _online(mock_agent_manager)
    mock_agent_manager._request.side_effect = _http_status_error(404)
    resp = orchestrator_client.get(
        "/api/integration/v1/agents/A/containers/missing/stats/history?window=900",
        headers=bearer,
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_integration_history_offline_agent_503(
    orchestrator_client, mock_agent_manager, bearer
):
    _online(mock_agent_manager, status="offline")
    resp = orchestrator_client.get(
        "/api/integration/v1/agents/A/containers/c1/stats/history?window=900",
        headers=bearer,
    )
    assert resp.status_code == 503
    assert resp.json()["code"] == "agent_offline"
    mock_agent_manager._request.assert_not_awaited()


def test_integration_history_unreachable_502(
    orchestrator_client, mock_agent_manager, bearer
):
    _online(mock_agent_manager)
    mock_agent_manager._request.side_effect = RuntimeError("connection refused")
    resp = orchestrator_client.get(
        "/api/integration/v1/agents/A/containers/c1/stats/history?window=900",
        headers=bearer,
    )
    assert resp.status_code == 502
    assert resp.json()["code"] == "agent_unreachable"


# ---------------------------------------------------------------------------
# Integration hot-set behaviour
# ---------------------------------------------------------------------------

def test_unit_stats_served_from_fresh_hot_sample(
    orchestrator_client, mock_agent_manager, bearer, monkeypatch
):
    _online(mock_agent_manager)
    fake = HotFakeStatsManager({("A", "web"): _sample()})
    monkeypatch.setattr(integration_mod, "_stats_manager", lambda: fake)

    resp = orchestrator_client.get(
        "/api/integration/v1/agents/A/containers/web/stats", headers=bearer
    )

    assert resp.status_code == 200
    body = resp.json()
    # Same normalisation as a live snapshot: raw 400 % / 4 vCPU -> 100 % host.
    assert body["cpu_percent"] == 100.0
    assert body["cpu_percent_raw"] == 400.0
    assert body["cpu_count"] == 4
    assert body["network_rx"] == 1234
    assert body["network_tx"] == 5678
    # checkedAt is the sample timestamp, not the request time.
    assert body["checkedAt"] == "2023-11-14T22:13:20Z"
    mock_agent_manager.fetch_containers_stats.assert_not_awaited()
    assert fake.hot_calls == [("A", ["web"])]


def test_unit_stats_falls_back_to_live_and_marks_hot_set(
    orchestrator_client, mock_agent_manager, bearer, monkeypatch
):
    _online(mock_agent_manager)
    mock_agent_manager.fetch_containers_stats.return_value = {
        "results": [
            {
                "found": True,
                "id": "abcdef123456",
                "name": "web",
                "state": "running",
                "health": "healthy",
                "cpu_percent": 400.0,
                "cpu_count": 4,
                "mem_usage": 600,
                "mem_limit": 10000,
                "mem_percent": 6.0,
                "mem_cache": 400,
                "network_rx": 1,
                "network_tx": 2,
            }
        ]
    }
    fake = HotFakeStatsManager()  # no fresh sample
    monkeypatch.setattr(integration_mod, "_stats_manager", lambda: fake)

    resp = orchestrator_client.get(
        "/api/integration/v1/agents/A/containers/web/stats", headers=bearer
    )

    assert resp.status_code == 200
    assert resp.json()["cpu_percent"] == 100.0
    mock_agent_manager.fetch_containers_stats.assert_awaited_once_with("A", ["web"])
    assert fake.hot_calls == [("A", ["web"])]


def test_batch_stats_serves_fresh_from_cache_and_fetches_the_rest(
    orchestrator_client, mock_agent_manager, bearer, monkeypatch
):
    _online(mock_agent_manager)
    fake = HotFakeStatsManager({("A", "web"): _sample()})
    monkeypatch.setattr(integration_mod, "_stats_manager", lambda: fake)
    mock_agent_manager.fetch_containers_stats.return_value = {
        "results": [{"found": False, "state": "unknown"}]
    }

    targets = [
        {"agent": "A", "container": "web"},
        {"agent": "A", "container": "missing"},
    ]
    resp = orchestrator_client.post(
        "/api/integration/v1/containers/stats", headers=bearer, json={"targets": targets}
    )

    assert resp.status_code == 200
    results = resp.json()["results"]
    # Cached target served without a live round-trip.
    assert results[0]["found"] is True
    assert results[0]["cpu_percent"] == 100.0
    assert results[0]["error"] is None
    # Uncached target still pays a live fetch and reports its error.
    assert results[1]["found"] is False
    assert results[1]["error"]["code"] == "not_found"
    mock_agent_manager.fetch_containers_stats.assert_awaited_once_with("A", ["missing"])
    assert fake.hot_calls == [("A", ["web", "missing"])]


def test_batch_stats_all_cached_skips_live_fetch(
    orchestrator_client, mock_agent_manager, bearer, monkeypatch
):
    _online(mock_agent_manager)
    fake = HotFakeStatsManager({("A", "web"): _sample()})
    monkeypatch.setattr(integration_mod, "_stats_manager", lambda: fake)

    resp = orchestrator_client.post(
        "/api/integration/v1/containers/stats",
        headers=bearer,
        json={"targets": [{"agent": "A", "container": "web"}]},
    )

    assert resp.status_code == 200
    assert resp.json()["results"][0]["found"] is True
    mock_agent_manager.fetch_containers_stats.assert_not_awaited()
    assert fake.hot_calls == [("A", ["web"])]


def test_hot_cache_exception_does_not_break_live_stats(
    orchestrator_client, mock_agent_manager, bearer, monkeypatch
):
    """A broken hot cache must degrade to the live snapshot, never 500."""
    _online(mock_agent_manager)
    mock_agent_manager.fetch_containers_stats.return_value = {
        "results": [
            {
                "found": True,
                "id": "abcdef123456",
                "name": "web",
                "state": "running",
                "health": None,
                "cpu_percent": 100.0,
                "cpu_count": 2,
                "mem_usage": 1,
                "mem_limit": 2,
                "mem_percent": 50.0,
                "mem_cache": 0,
                "network_rx": 0,
                "network_tx": 0,
            }
        ]
    }

    class BrokenStatsManager:
        async def mark_hot_set(self, agent, containers):
            raise RuntimeError("hot set down")

        def get_fresh_sample(self, agent, container, max_age=None):
            raise RuntimeError("cache down")

    monkeypatch.setattr(integration_mod, "_stats_manager", lambda: BrokenStatsManager())

    resp = orchestrator_client.get(
        "/api/integration/v1/agents/A/containers/web/stats", headers=bearer
    )

    assert resp.status_code == 200
    assert resp.json()["state"] == "running"
    mock_agent_manager.fetch_containers_stats.assert_awaited_once_with("A", ["web"])


def test_integration_contract_unchanged_without_hot_cache(
    orchestrator_client, mock_agent_manager, bearer
):
    """Non-regression: with the real (empty) hot set the live path is used."""
    _online(mock_agent_manager)
    mock_agent_manager.fetch_containers_stats.return_value = {
        "results": [
            {
                "found": True,
                "id": "abcdef123456",
                "name": "web",
                "state": "running",
                "health": "healthy",
                "cpu_percent": 200.0,
                "cpu_count": 2,
                "mem_usage": 5,
                "mem_limit": 10,
                "mem_percent": 50.0,
                "mem_cache": 1,
                "network_rx": 7,
                "network_tx": 8,
            }
        ]
    }
    resp = orchestrator_client.get(
        "/api/integration/v1/agents/A/containers/web/stats", headers=bearer
    )
    assert resp.status_code == 200
    assert resp.json()["cpu_percent"] == 100.0
    mock_agent_manager.fetch_containers_stats.assert_awaited_once_with("A", ["web"])
