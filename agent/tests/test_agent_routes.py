"""Endpoint tests for the agent FastAPI routes.

All docker_manager backends are monkeypatched: the tests never touch a real
Docker daemon, git, registry or filesystem outside the test data dir.
"""

import pytest

from agent import docker_manager as dm


def _fake_stream():
    """Build an async stream generator producing one output + one done event."""

    async def _stream(name, idle_timeout=120):
        yield {"type": dm.STREAM_EVENT_OUTPUT, "line": "hello"}
        yield {"type": dm.STREAM_EVENT_RESULT, "success": True, "output": "hello", "error": ""}

    return _stream


# ---------------------------------------------------------------------------
# Health (no auth)
# ---------------------------------------------------------------------------

def test_health_requires_no_auth(agent_client):
    resp = agent_client.get("/agent/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------

def test_protected_endpoint_without_key_is_401(agent_client):
    resp = agent_client.get("/agent/containers")
    assert resp.status_code == 401


def test_protected_endpoint_with_bad_key_is_401(agent_client):
    resp = agent_client.get(
        "/agent/containers", headers={"Authorization": "Bearer wrong-key"}
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------

def test_list_containers_with_key(agent_client, api_key_header, monkeypatch):
    expected = [
        {"id": "abc123", "name": "web", "status": "running"},
        {"id": "def456", "name": "db", "status": "stopped"},
    ]
    monkeypatch.setattr(dm, "list_containers", lambda all=True: expected)

    resp = agent_client.get("/agent/containers", headers=api_key_header)

    assert resp.status_code == 200
    assert resp.json() == expected


def test_get_container_not_found(agent_client, api_key_header, monkeypatch):
    monkeypatch.setattr(dm, "get_container", lambda container_id: None)

    resp = agent_client.get("/agent/containers/missing123", headers=api_key_header)

    assert resp.status_code == 404
    assert "error" in resp.json()


def test_get_container_found(agent_client, api_key_header, monkeypatch):
    container = {
        "id": "abc123",
        "name": "web",
        "image": "nginx:latest",
        "status": "running",
    }
    monkeypatch.setattr(dm, "get_container", lambda container_id: dict(container))
    monkeypatch.setattr(dm, "get_container_stats", lambda container_id: {"cpu_percent": 0.0})

    resp = agent_client.get("/agent/containers/abc123", headers=api_key_header)

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "abc123"
    assert body["stats"]["cpu_percent"] == 0.0


@pytest.mark.parametrize(
    "path, fn_name",
    [
        ("/agent/containers/abc123/disable", "disable_container"),
        ("/agent/containers/abc123/delete", "delete_container"),
    ],
)
def test_container_disable_delete_routes(agent_client, api_key_header, monkeypatch, path, fn_name):
    monkeypatch.setattr(dm, fn_name, lambda container_id: True)

    resp = agent_client.post(path, headers=api_key_header)

    assert resp.status_code == 200
    assert resp.json() == {"success": True}


@pytest.mark.parametrize(
    "path",
    [
        "/agent/containers/abc123/disable",
        "/agent/containers/abc123/delete",
    ],
)
def test_container_disable_delete_require_auth(agent_client, path):
    resp = agent_client.post(path)
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Stacks, stack files, ports
# ---------------------------------------------------------------------------

def test_list_stacks(agent_client, api_key_header, monkeypatch):
    monkeypatch.setattr(
        dm, "list_stacks",
        lambda: [{"name": "myapp", "managed": True, "standalone": False}],
    )
    monkeypatch.setattr(dm, "list_containers", lambda all=True: [])

    resp = agent_client.get("/agent/stacks", headers=api_key_header)

    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
    assert resp.json()[0]["name"] == "myapp"


def test_get_stack_files(agent_client, api_key_header, monkeypatch):
    files = [
        {"name": "docker-compose.yml", "size": 42, "is_dir": False},
        {"name": ".env", "size": 7, "is_dir": False},
    ]
    monkeypatch.setattr(dm, "get_stack_files", lambda name, include_hidden=False: files)

    resp = agent_client.get("/agent/stacks/myapp/files", headers=api_key_header)

    assert resp.status_code == 200
    assert resp.json() == {"files": files}


def test_get_ports(agent_client, api_key_header, monkeypatch):
    ports = [{"port": "8080", "source": "docker", "container": "web", "stack": "myapp"}]
    monkeypatch.setattr(dm, "get_used_ports", lambda: ports)

    resp = agent_client.get("/agent/ports", headers=api_key_header)

    assert resp.status_code == 200
    assert resp.json() == ports


# ---------------------------------------------------------------------------
# SSE stack actions (start / stop / restart / update / deploy)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path, stream_name",
    [
        ("/agent/stacks/myapp/start", "stream_start_stack"),
        ("/agent/stacks/myapp/stop", "stream_stop_stack"),
        ("/agent/stacks/myapp/restart", "stream_restart_stack"),
        ("/agent/stacks/myapp/update", "stream_update_stack"),
        ("/agent/stacks/myapp/deploy", "stream_deploy_stack"),
        ("/agent/stacks/myapp/down", "stream_down_stack"),
    ],
)
def test_stack_sse_actions(agent_client, api_key_header, monkeypatch, path, stream_name):
    monkeypatch.setattr(dm, stream_name, _fake_stream())

    resp = agent_client.post(path, headers=api_key_header)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    body = resp.text
    assert "event: output" in body
    assert "event: done" in body
    assert '"line": "hello"' in body


@pytest.mark.parametrize(
    "path",
    [
        "/agent/stacks/myapp/start",
        "/agent/stacks/myapp/stop",
        "/agent/stacks/myapp/restart",
        "/agent/stacks/myapp/update",
        "/agent/stacks/myapp/deploy",
        "/agent/stacks/myapp/down",
    ],
)
def test_stack_sse_actions_require_auth(agent_client, path):
    resp = agent_client.post(path)
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Batch stats (LOT B)
# ---------------------------------------------------------------------------

def test_batch_stats_requires_auth(agent_client):
    resp = agent_client.post("/agent/containers/stats", json={"ids": ["web"]})
    assert resp.status_code == 401


def test_batch_stats_returns_results_in_order_with_partial_errors(
    agent_client, api_key_header, monkeypatch
):
    captured = {}

    def _fake(refs):
        captured["refs"] = list(refs)
        return [
            {"found": True, "id": "c1", "name": "web", "state": "running", "cpu_percent": 10.0},
            {"found": False, "id": "", "name": "", "state": "unknown", "error": "Container not found"},
        ]

    monkeypatch.setattr(dm, "get_containers_stats", _fake)

    resp = agent_client.post(
        "/agent/containers/stats",
        headers=api_key_header,
        json={"ids": ["web", "missing"]},
    )

    assert resp.status_code == 200
    assert captured["refs"] == ["web", "missing"]
    body = resp.json()
    assert [r["found"] for r in body["results"]] == [True, False]
    assert body["results"][0]["state"] == "running"
    assert body["results"][1]["error"] == "Container not found"


def test_batch_stats_accepts_names_alias(agent_client, api_key_header, monkeypatch):
    captured = {}

    def _fake(refs):
        captured["refs"] = list(refs)
        return []

    monkeypatch.setattr(dm, "get_containers_stats", _fake)

    resp = agent_client.post(
        "/agent/containers/stats",
        headers=api_key_header,
        json={"names": ["web"]},
    )

    assert resp.status_code == 200
    assert captured["refs"] == ["web"]


@pytest.mark.parametrize("payload", [{"ids": "nope"}, {"ids": [1, 2]}, {}, []])
def test_batch_stats_invalid_body_400(agent_client, api_key_header, payload):
    resp = agent_client.post("/agent/containers/stats", headers=api_key_header, json=payload)
    assert resp.status_code == 400


def test_batch_stats_too_many_400(agent_client, api_key_header):
    refs = [f"c{i}" for i in range(dm.MAX_BATCH_STATS + 1)]
    resp = agent_client.post(
        "/agent/containers/stats", headers=api_key_header, json={"ids": refs}
    )
    assert resp.status_code == 400


def test_batch_stats_invalid_json_400(agent_client, api_key_header):
    resp = agent_client.post(
        "/agent/containers/stats",
        headers={**api_key_header, "Content-Type": "application/json"},
        content=b"{not json",
    )
    assert resp.status_code == 400
