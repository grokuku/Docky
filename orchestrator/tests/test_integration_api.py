"""Tests for the Docky↔Homy integration façade (``/api/integration/v1``).

Covers (LOT A):

- dedicated Bearer auth (401 missing/wrong, 403 malformed scheme, 503 when the
  façade is disabled or the key is not configured);
- CSRF exemption for ``/api/integration/`` mutants (Bearer POST without any
  cookie/CSRF material must reach the route);
- agents listing (version / lastCheck capture);
- container normalisation (``health`` null→none, unknown ``state``→unknown);
- single container by name AND by id (+ 404);
- batch health (order preserved, per-target errors, 100/256 KiB limits);
- the integration API key settings endpoints (clear display + regeneration).
"""

import json

import httpx
import pytest
import yaml

from orchestrator.tests._helpers import make_settings


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def integration_key(orchestrator_client, data_dir):
    """Configure an integration key AFTER ``orchestrator_client`` seeded config."""
    from app.routes.integration import get_integration_api_key

    return get_integration_api_key()


@pytest.fixture
def bearer(integration_key):
    """Authorization header carrying the valid integration key."""
    return {"Authorization": f"Bearer {integration_key}"}


@pytest.fixture
def csrf_enabled(monkeypatch):
    """Re-enable CSRF enforcement for one test (deletes the suite bypass)."""
    from app.auth import csrf as csrf_mod

    monkeypatch.delenv(csrf_mod.TEST_BYPASS_ENV_VAR, raising=False)
    return csrf_mod


def _set_integration_enabled(enabled: bool):
    from app.config import load_settings, save_settings

    settings = load_settings()
    settings.setdefault("security", {})["integration_enabled"] = enabled
    save_settings(settings)


def _container(cid="c1", name="web", **overrides):
    data = {
        "id": cid,
        "name": name,
        "image": "nginx:latest",
        "state": "running",
        "health": None,
        "stack": None,
        "service": "web",
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# Normalisation helpers (unit)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "none"),
        ("healthy", "healthy"),
        ("UNHEALTHY", "unhealthy"),
        ("starting", "starting"),
        ("none", "none"),
        ("weird", "none"),
    ],
)
def test_normalize_health(value, expected):
    from app.routes.integration import normalize_health

    assert normalize_health(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("running", "running"),
        ("Exited", "exited"),
        ("removing", "unknown"),
        ("stopping", "unknown"),
        (None, "unknown"),
        ("", "unknown"),
    ],
)
def test_normalize_state(value, expected):
    from app.routes.integration import normalize_state

    assert normalize_state(value) == expected


@pytest.mark.parametrize(
    "raw,count,expected",
    [
        (400.0, 4, 100.0),
        (200.0, 8, 25.0),
        (50.0, 1, 50.0),
        (-5.0, 4, 0.0),
        (1000.0, 4, 100.0),
        (None, 4, 0.0),
        ("bad", 4, 0.0),
        (200.0, 0, 100.0),
    ],
)
def test_normalize_cpu_percent(raw, count, expected):
    from app.routes.integration import normalize_cpu_percent

    assert normalize_cpu_percent(raw, count) == expected


# ---------------------------------------------------------------------------
# API key management
# ---------------------------------------------------------------------------

def test_get_integration_api_key_generates_and_persists(data_dir):
    from app.routes.integration import get_integration_api_key

    make_settings(data_dir)
    key = get_integration_api_key()
    assert len(key) >= 32
    assert get_integration_api_key() == key
    settings = yaml.safe_load((data_dir / "settings.yaml").read_text(encoding="utf-8"))
    assert settings["security"]["integration_api_key"] == key


def test_get_integration_api_key_is_distinct_from_mcp(data_dir):
    from app.mcp_server import get_mcp_api_key
    from app.routes.integration import get_integration_api_key

    make_settings(data_dir)
    assert get_integration_api_key() != get_mcp_api_key()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_integration_missing_header_401(orchestrator_client, integration_key):
    resp = orchestrator_client.get("/api/integration/v1/agents")
    assert resp.status_code == 401
    assert resp.json() == {"error": "Invalid or missing API key", "code": "unauthorized"}


def test_integration_wrong_token_401(orchestrator_client, integration_key):
    resp = orchestrator_client.get(
        "/api/integration/v1/agents",
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"


def test_integration_non_bearer_scheme_403(orchestrator_client, integration_key):
    resp = orchestrator_client.get(
        "/api/integration/v1/agents",
        headers={"Authorization": f"Basic {integration_key}"},
    )
    assert resp.status_code == 403
    assert resp.json() == {"error": "Forbidden", "code": "forbidden"}


def test_integration_not_configured_503(orchestrator_client):
    # No key configured yet (orchestrator_client seeds a keyless settings.yaml).
    resp = orchestrator_client.get(
        "/api/integration/v1/agents",
        headers={"Authorization": "Bearer anything"},
    )
    assert resp.status_code == 503
    assert resp.json() == {"error": "Docky is not configured", "code": "not_configured"}


def test_integration_disabled_503(orchestrator_client, integration_key, bearer):
    _set_integration_enabled(False)
    resp = orchestrator_client.get("/api/integration/v1/agents", headers=bearer)
    assert resp.status_code == 503
    assert resp.json()["code"] == "not_configured"


# ---------------------------------------------------------------------------
# CSRF exemption (Bearer mutants must not require a CSRF cookie)
# ---------------------------------------------------------------------------

def test_batch_health_csrf_exempt_with_bearer(
    orchestrator_client, mock_agent_manager, bearer, csrf_enabled
):
    mock_agent_manager.agents = {
        "Test Agent": {"url": "http://agent:8080", "status": "online", "last_check": 0}
    }
    mock_agent_manager.get_containers.return_value = [_container("c1", "web")]

    # Control: a non-exempt /api mutant without CSRF material is rejected.
    control = orchestrator_client.post("/api/presence/heartbeat")
    assert control.status_code == 403

    # The integration mutant carries only the Bearer header → must reach 200.
    resp = orchestrator_client.post(
        "/api/integration/v1/containers/health",
        headers=bearer,
        json={"targets": [{"agent": "Test Agent", "container": "c1"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["results"][0]["found"] is True


def test_batch_health_without_bearer_csrf_enabled_401(orchestrator_client, integration_key, csrf_enabled):
    # Exempt from CSRF, but still requires the Bearer key → 401 (not 403 CSRF).
    resp = orchestrator_client.post(
        "/api/integration/v1/containers/health",
        json={"targets": []},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

def test_list_agents_with_version_and_last_check(orchestrator_client, mock_agent_manager, bearer):
    mock_agent_manager.agents = {
        "Alpha": {
            "url": "http://alpha:8080",
            "status": "online",
            "version": "1.2.3",
            "last_check": 1_700_000_000.0,
        }
    }
    resp = orchestrator_client.get("/api/integration/v1/agents", headers=bearer)
    assert resp.status_code == 200
    assert resp.json() == {
        "agents": [
            {
                "name": "Alpha",
                "url": "http://alpha:8080",
                "status": "online",
                "version": "1.2.3",
                "lastCheck": "2023-11-14T22:13:20Z",
            }
        ]
    }


def test_list_agents_no_agents_503(orchestrator_client, mock_agent_manager, bearer):
    mock_agent_manager.agents = {}
    resp = orchestrator_client.get("/api/integration/v1/agents", headers=bearer)
    assert resp.status_code == 503
    assert resp.json() == {"error": "No agents configured", "code": "no_agents"}


# ---------------------------------------------------------------------------
# Containers (list + normalisation)
# ---------------------------------------------------------------------------

def test_list_containers_normalised(orchestrator_client, mock_agent_manager, bearer):
    mock_agent_manager.agents = {
        "A": {"url": "http://a:8080", "status": "online", "last_check": 0}
    }
    mock_agent_manager.get_containers.return_value = [
        _container("c1", "web", state="running", health=None, stack=None, service="web"),
        _container("c2", "db", state="removing", health="healthy", stack="MyApp", service="db"),
    ]
    resp = orchestrator_client.get("/api/integration/v1/agents/A/containers", headers=bearer)
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent"] == "A"
    assert body["containers"] == [
        {
            "id": "c1",
            "name": "web",
            "image": "nginx:latest",
            "state": "running",
            "health": "none",
            "stack": None,
            "service": "web",
        },
        {
            "id": "c2",
            "name": "db",
            "image": "nginx:latest",
            "state": "unknown",
            "health": "healthy",
            "stack": "MyApp",
            "service": "db",
        },
    ]


def test_list_containers_unknown_agent_404(orchestrator_client, mock_agent_manager, bearer):
    resp = orchestrator_client.get("/api/integration/v1/agents/ghost/containers", headers=bearer)
    assert resp.status_code == 404
    assert resp.json()["code"] == "agent_not_found"


def test_list_containers_offline_agent_502(orchestrator_client, mock_agent_manager, bearer):
    mock_agent_manager.agents = {
        "A": {"url": "http://a:8080", "status": "offline", "last_check": 0}
    }
    resp = orchestrator_client.get("/api/integration/v1/agents/A/containers", headers=bearer)
    assert resp.status_code == 502
    assert resp.json()["code"] == "agent_unreachable"


# ---------------------------------------------------------------------------
# Single container (by name AND by id)
# ---------------------------------------------------------------------------

def test_get_container_by_name(orchestrator_client, mock_agent_manager, bearer):
    mock_agent_manager.agents = {
        "A": {"url": "http://a:8080", "status": "online", "last_check": 0}
    }
    mock_agent_manager.get_container.return_value = _container("c1", "web", health=None)
    resp = orchestrator_client.get("/api/integration/v1/agents/A/containers/web", headers=bearer)
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent"] == "A"
    assert body["container"] == "web"
    assert body["id"] == "c1"
    assert body["name"] == "web"
    assert body["state"] == "running"
    assert body["health"] == "none"
    assert body["checkedAt"].endswith("Z")
    mock_agent_manager.get_container.assert_awaited_once_with("A", "web")


def test_get_container_by_id(orchestrator_client, mock_agent_manager, bearer):
    mock_agent_manager.agents = {
        "A": {"url": "http://a:8080", "status": "online", "last_check": 0}
    }
    mock_agent_manager.get_container.return_value = _container("c1", "web")
    resp = orchestrator_client.get("/api/integration/v1/agents/A/containers/c1", headers=bearer)
    assert resp.status_code == 200
    assert resp.json()["id"] == "c1"
    mock_agent_manager.get_container.assert_awaited_once_with("A", "c1")


def test_get_container_not_found_404(orchestrator_client, mock_agent_manager, bearer):
    mock_agent_manager.agents = {
        "A": {"url": "http://a:8080", "status": "online", "last_check": 0}
    }
    mock_agent_manager.get_container.return_value = None
    resp = orchestrator_client.get("/api/integration/v1/agents/A/containers/nope", headers=bearer)
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_get_container_unknown_agent_404(orchestrator_client, mock_agent_manager, bearer):
    resp = orchestrator_client.get("/api/integration/v1/agents/ghost/containers/c1", headers=bearer)
    assert resp.status_code == 404
    assert resp.json()["code"] == "agent_not_found"


def test_get_container_offline_agent_503(orchestrator_client, mock_agent_manager, bearer):
    mock_agent_manager.agents = {
        "A": {"url": "http://a:8080", "status": "offline", "last_check": 0}
    }
    resp = orchestrator_client.get("/api/integration/v1/agents/A/containers/c1", headers=bearer)
    assert resp.status_code == 503
    assert resp.json()["code"] == "agent_offline"


# ---------------------------------------------------------------------------
# Batch health
# ---------------------------------------------------------------------------

def test_batch_health_preserves_order_and_per_target_errors(
    orchestrator_client, mock_agent_manager, bearer
):
    mock_agent_manager.agents = {
        "A": {"url": "http://a:8080", "status": "online", "last_check": 0},
        "B": {"url": "http://b:8080", "status": "offline", "last_check": 0},
    }
    mock_agent_manager.get_containers.return_value = [
        _container("abcdef1234", "web", state="running", health="healthy")
    ]
    targets = [
        {"agent": "A", "container": "web"},       # found (by name)
        {"agent": "A", "container": "abcdef1234"},  # found (by id)
        {"agent": "A", "container": "abcdef"},      # found (id prefix)
        {"agent": "A", "container": "missing"},      # not_found
        {"agent": "B", "container": "whatever"},     # agent offline → unreachable
        {"agent": "ghost", "container": "x"},         # unknown agent → unreachable
    ]
    resp = orchestrator_client.post(
        "/api/integration/v1/containers/health",
        headers=bearer,
        json={"targets": targets},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["checkedAt"].endswith("Z")
    results = body["results"]
    assert len(results) == len(targets)
    assert [r["container"] for r in results] == [t["container"] for t in targets]
    assert results[0]["found"] is True and results[0]["state"] == "running"
    assert results[0]["health"] == "healthy"
    assert results[1]["found"] is True
    assert results[2]["found"] is True
    assert results[3]["found"] is False
    assert results[3]["state"] == "unknown"
    assert results[3]["error"]["code"] == "not_found"
    assert results[4]["found"] is False
    assert results[4]["error"]["code"] == "agent_unreachable"
    assert results[5]["found"] is False
    assert results[5]["error"]["code"] == "agent_unreachable"
    # One fetch per distinct agent at most.
    assert mock_agent_manager.get_containers.await_count == 1


def test_batch_health_too_many_targets_400(orchestrator_client, mock_agent_manager, bearer):
    targets = [{"agent": "A", "container": f"c{i}"} for i in range(101)]
    resp = orchestrator_client.post(
        "/api/integration/v1/containers/health",
        headers=bearer,
        json={"targets": targets},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_request"


def test_batch_health_body_too_large_400(orchestrator_client, mock_agent_manager, bearer):
    payload = json.dumps({"targets": [], "padding": "x" * (256 * 1024 + 1)}).encode()
    resp = orchestrator_client.post(
        "/api/integration/v1/containers/health",
        headers={**bearer, "Content-Type": "application/json"},
        content=payload,
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_request"


def test_batch_health_invalid_json_400(orchestrator_client, mock_agent_manager, bearer):
    resp = orchestrator_client.post(
        "/api/integration/v1/containers/health",
        headers={**bearer, "Content-Type": "application/json"},
        content=b"{not json",
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_request"


def test_batch_health_targets_not_list_400(orchestrator_client, mock_agent_manager, bearer):
    resp = orchestrator_client.post(
        "/api/integration/v1/containers/health",
        headers=bearer,
        json={"targets": "nope"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_request"


def test_batch_health_empty_targets_200(orchestrator_client, mock_agent_manager, bearer):
    resp = orchestrator_client.post(
        "/api/integration/v1/containers/health",
        headers=bearer,
        json={"targets": []},
    )
    assert resp.status_code == 200
    assert resp.json()["results"] == []


# ---------------------------------------------------------------------------
# Settings (key display + regeneration)
# ---------------------------------------------------------------------------

def test_settings_integration_requires_auth(orchestrator_client):
    resp = orchestrator_client.get("/api/settings/integration")
    assert resp.status_code == 401
    resp = orchestrator_client.post("/api/settings/integration/regenerate")
    assert resp.status_code == 401


def test_settings_integration_returns_clear_key(auth_client):
    resp = auth_client.get("/api/settings/integration")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["api_key"]
    assert not body["api_key"].startswith("****")


def test_regenerate_integration_key_persists(auth_client, data_dir):
    first = auth_client.get("/api/settings/integration").json()["api_key"]
    resp = auth_client.post("/api/settings/integration/regenerate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["api_key"] and body["api_key"] != first
    settings = yaml.safe_load((data_dir / "settings.yaml").read_text(encoding="utf-8"))
    assert settings["security"]["integration_api_key"] == body["api_key"]


def test_settings_page_renders_integration_card(auth_client):
    resp = auth_client.get("/settings")
    assert resp.status_code == 200
    assert "API d'intégration" in resp.text
    assert 'id="integration-api-key"' in resp.text


# ---------------------------------------------------------------------------
# LOT B — stats helpers
# ---------------------------------------------------------------------------

def _online(manager, name="A", status="online"):
    manager.agents = {
        name: {"url": f"http://{name.lower()}:8080", "status": status, "last_check": 0}
    }
    return manager


def _agent_stats(**overrides):
    """A representative agent batch-stats result (raw, multi-core CPU)."""
    data = {
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
        "network_rx": 1234,
        "network_tx": 5678,
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# LOT B — single stats
# ---------------------------------------------------------------------------

def test_get_container_stats_unit(orchestrator_client, mock_agent_manager, bearer):
    _online(mock_agent_manager)
    mock_agent_manager.fetch_containers_stats.return_value = {"results": [_agent_stats()]}

    resp = orchestrator_client.get(
        "/api/integration/v1/agents/A/containers/web/stats", headers=bearer
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["agent"] == "A"
    assert body["container"] == "web"
    assert body["state"] == "running"
    assert body["health"] == "healthy"
    # 400 % multi-core / 4 vCPU -> 100 % host
    assert body["cpu_percent"] == 100.0
    assert body["cpu_percent_raw"] == 400.0
    assert body["cpu_count"] == 4
    assert body["mem_usage"] == 600
    assert body["mem_limit"] == 10000
    assert body["mem_percent"] == 6.0
    assert body["mem_cache"] == 400
    assert body["network_rx"] == 1234
    assert body["network_tx"] == 5678
    assert body["disk_usage"] is None
    assert body["disk_limit"] is None
    assert body["disk_percent"] is None
    assert body["checkedAt"].endswith("Z")
    mock_agent_manager.fetch_containers_stats.assert_awaited_once_with("A", ["web"])


def test_get_container_stats_by_id(orchestrator_client, mock_agent_manager, bearer):
    _online(mock_agent_manager)
    mock_agent_manager.fetch_containers_stats.return_value = {
        "results": [_agent_stats(id="abcdef123456", name="web")]
    }

    resp = orchestrator_client.get(
        "/api/integration/v1/agents/A/containers/abcdef123456/stats", headers=bearer
    )

    assert resp.status_code == 200
    mock_agent_manager.fetch_containers_stats.assert_awaited_once_with("A", ["abcdef123456"])


def test_get_container_stats_stopped_returns_200_zeroed(
    orchestrator_client, mock_agent_manager, bearer
):
    _online(mock_agent_manager)
    mock_agent_manager.fetch_containers_stats.return_value = {
        "results": [
            _agent_stats(
                state="exited",
                health=None,
                cpu_percent=0.0,
                mem_usage=0,
                mem_percent=0.0,
                mem_cache=0,
                network_rx=0,
                network_tx=0,
                mem_limit=10000,
            )
        ]
    }

    resp = orchestrator_client.get(
        "/api/integration/v1/agents/A/containers/web/stats", headers=bearer
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "exited"
    assert body["health"] == "none"
    assert body["cpu_percent"] == 0.0
    assert body["mem_usage"] == 0
    assert body["network_rx"] == 0


def test_get_container_stats_not_found_404(orchestrator_client, mock_agent_manager, bearer):
    _online(mock_agent_manager)
    mock_agent_manager.fetch_containers_stats.return_value = {
        "results": [{"found": False, "state": "unknown", "error": "Container not found"}]
    }

    resp = orchestrator_client.get(
        "/api/integration/v1/agents/A/containers/missing/stats", headers=bearer
    )

    assert resp.status_code == 404
    assert resp.json() == {
        "error": "Container 'missing' not found on agent 'A'",
        "code": "not_found",
    }


def test_get_container_stats_unknown_agent_404(orchestrator_client, mock_agent_manager, bearer):
    _online(mock_agent_manager)
    resp = orchestrator_client.get(
        "/api/integration/v1/agents/ghost/containers/web/stats", headers=bearer
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "agent_not_found"


def test_get_container_stats_offline_agent_503(orchestrator_client, mock_agent_manager, bearer):
    _online(mock_agent_manager, status="offline")
    resp = orchestrator_client.get(
        "/api/integration/v1/agents/A/containers/web/stats", headers=bearer
    )
    assert resp.status_code == 503
    assert resp.json()["code"] == "agent_offline"
    mock_agent_manager.fetch_containers_stats.assert_not_awaited()


def test_get_container_stats_unreachable_agent_502(
    orchestrator_client, mock_agent_manager, bearer
):
    _online(mock_agent_manager)
    mock_agent_manager.fetch_containers_stats.side_effect = RuntimeError("connection refused")

    resp = orchestrator_client.get(
        "/api/integration/v1/agents/A/containers/web/stats", headers=bearer
    )

    assert resp.status_code == 502
    assert resp.json()["code"] == "agent_unreachable"


def test_get_container_stats_timeout_504(orchestrator_client, mock_agent_manager, bearer):
    _online(mock_agent_manager)
    mock_agent_manager.fetch_containers_stats.side_effect = httpx.ReadTimeout("slow")

    resp = orchestrator_client.get(
        "/api/integration/v1/agents/A/containers/web/stats", headers=bearer
    )

    assert resp.status_code == 504
    assert resp.json()["code"] == "timeout"


# ---------------------------------------------------------------------------
# LOT B — batch stats
# ---------------------------------------------------------------------------

def test_batch_stats_preserves_order_and_per_target_errors(
    orchestrator_client, mock_agent_manager, bearer
):
    mock_agent_manager.agents = {
        "A": {"url": "http://a:8080", "status": "online", "last_check": 0},
        "B": {"url": "http://b:8080", "status": "offline", "last_check": 0},
    }
    mock_agent_manager.fetch_containers_stats.return_value = {
        "results": [_agent_stats(), {"found": False, "state": "unknown"}]
    }

    targets = [
        {"agent": "A", "container": "web"},
        {"agent": "A", "container": "missing"},
        {"agent": "B", "container": "whatever"},
        {"agent": "ghost", "container": "x"},
    ]
    resp = orchestrator_client.post(
        "/api/integration/v1/containers/stats", headers=bearer, json={"targets": targets}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["checkedAt"].endswith("Z")
    results = body["results"]
    assert [r["container"] for r in results] == [t["container"] for t in targets]
    assert results[0]["found"] is True
    assert results[0]["cpu_percent"] == 100.0
    assert results[0]["error"] is None
    assert results[0]["disk_usage"] is None
    assert results[1]["found"] is False
    assert results[1]["error"]["code"] == "not_found"
    assert results[1]["mem_usage"] == 0
    assert results[2]["found"] is False
    assert results[2]["error"]["code"] == "agent_unreachable"
    assert results[3]["found"] is False
    assert results[3]["error"]["code"] == "agent_unreachable"
    # Only agent A is online/known -> a single grouped call.
    assert mock_agent_manager.fetch_containers_stats.await_count == 1
    mock_agent_manager.fetch_containers_stats.assert_awaited_once_with("A", ["web", "missing"])


def test_batch_stats_agent_timeout_marks_targets(orchestrator_client, mock_agent_manager, bearer):
    _online(mock_agent_manager)
    mock_agent_manager.fetch_containers_stats.side_effect = httpx.ReadTimeout("slow")
    resp = orchestrator_client.post(
        "/api/integration/v1/containers/stats",
        headers=bearer,
        json={"targets": [{"agent": "A", "container": "web"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["results"][0]["error"]["code"] == "timeout"


def test_batch_stats_too_many_targets_400(orchestrator_client, mock_agent_manager, bearer):
    targets = [{"agent": "A", "container": f"c{i}"} for i in range(101)]
    resp = orchestrator_client.post(
        "/api/integration/v1/containers/stats", headers=bearer, json={"targets": targets}
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_request"


def test_batch_stats_body_too_large_400(orchestrator_client, mock_agent_manager, bearer):
    payload = json.dumps({"targets": [], "padding": "x" * (256 * 1024 + 1)}).encode()
    resp = orchestrator_client.post(
        "/api/integration/v1/containers/stats",
        headers={**bearer, "Content-Type": "application/json"},
        content=payload,
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_request"


def test_batch_stats_invalid_json_400(orchestrator_client, mock_agent_manager, bearer):
    resp = orchestrator_client.post(
        "/api/integration/v1/containers/stats",
        headers={**bearer, "Content-Type": "application/json"},
        content=b"{not json",
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_request"


def test_batch_stats_empty_targets_200(orchestrator_client, mock_agent_manager, bearer):
    resp = orchestrator_client.post(
        "/api/integration/v1/containers/stats", headers=bearer, json={"targets": []}
    )
    assert resp.status_code == 200
    assert resp.json()["results"] == []


# ---------------------------------------------------------------------------
# LOT B — actions
# ---------------------------------------------------------------------------

def test_action_start_success_returns_resulting_state(
    orchestrator_client, mock_agent_manager, bearer
):
    _online(mock_agent_manager)
    mock_agent_manager.fetch_containers_stats.side_effect = [
        {"results": [_agent_stats(state="exited", health=None)]},
        {"results": [_agent_stats(state="running", health="starting")]},
    ]
    mock_agent_manager.action_container.return_value = {"success": True}

    resp = orchestrator_client.post(
        "/api/integration/v1/agents/A/containers/web/start", headers=bearer
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "success": True,
        "agent": "A",
        "container": "web",
        "action": "start",
        "state": "running",
        "health": "starting",
    }
    mock_agent_manager.action_container.assert_awaited_once_with("A", "web", "start")


def test_action_restart_success(orchestrator_client, mock_agent_manager, bearer):
    _online(mock_agent_manager)
    mock_agent_manager.fetch_containers_stats.side_effect = [
        {"results": [_agent_stats(state="running", health="healthy")]},
        {"results": [_agent_stats(state="running", health="starting")]},
    ]
    mock_agent_manager.action_container.return_value = {"success": True}

    resp = orchestrator_client.post(
        "/api/integration/v1/agents/A/containers/web/restart", headers=bearer
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "restart"
    assert body["state"] == "running"
    assert body["health"] == "starting"


def test_action_stop_success(orchestrator_client, mock_agent_manager, bearer):
    _online(mock_agent_manager)
    mock_agent_manager.fetch_containers_stats.side_effect = [
        {"results": [_agent_stats(state="running", health="healthy")]},
        {"results": [_agent_stats(state="exited", health=None)]},
    ]
    mock_agent_manager.action_container.return_value = {"success": True}

    resp = orchestrator_client.post(
        "/api/integration/v1/agents/A/containers/web/stop", headers=bearer
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "exited"


def test_action_start_already_running_409(orchestrator_client, mock_agent_manager, bearer):
    _online(mock_agent_manager)
    mock_agent_manager.fetch_containers_stats.return_value = {
        "results": [_agent_stats(state="running")]
    }

    resp = orchestrator_client.post(
        "/api/integration/v1/agents/A/containers/web/start", headers=bearer
    )

    assert resp.status_code == 409
    assert resp.json() == {
        "error": "Container is already running",
        "code": "conflict",
        "state": "running",
    }
    mock_agent_manager.action_container.assert_not_awaited()


@pytest.mark.parametrize("state", ["exited", "dead", "created"])
def test_action_stop_already_stopped_409(
    orchestrator_client, mock_agent_manager, bearer, state
):
    _online(mock_agent_manager)
    mock_agent_manager.fetch_containers_stats.return_value = {
        "results": [_agent_stats(state=state)]
    }

    resp = orchestrator_client.post(
        "/api/integration/v1/agents/A/containers/web/stop", headers=bearer
    )

    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "Container is already stopped"
    assert body["code"] == "conflict"
    assert body["state"] == state
    mock_agent_manager.action_container.assert_not_awaited()


def test_action_unknown_container_404(orchestrator_client, mock_agent_manager, bearer):
    _online(mock_agent_manager)
    mock_agent_manager.fetch_containers_stats.return_value = {
        "results": [{"found": False, "state": "unknown"}]
    }

    resp = orchestrator_client.post(
        "/api/integration/v1/agents/A/containers/missing/start", headers=bearer
    )

    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"
    mock_agent_manager.action_container.assert_not_awaited()


def test_action_unknown_agent_404(orchestrator_client, mock_agent_manager, bearer):
    _online(mock_agent_manager)
    resp = orchestrator_client.post(
        "/api/integration/v1/agents/ghost/containers/web/start", headers=bearer
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "agent_not_found"


def test_action_unknown_action_404(orchestrator_client, mock_agent_manager, bearer):
    _online(mock_agent_manager)
    resp = orchestrator_client.post(
        "/api/integration/v1/agents/A/containers/web/destroy", headers=bearer
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_action_offline_agent_503(orchestrator_client, mock_agent_manager, bearer):
    _online(mock_agent_manager, status="offline")
    resp = orchestrator_client.post(
        "/api/integration/v1/agents/A/containers/web/start", headers=bearer
    )
    assert resp.status_code == 503
    assert resp.json()["code"] == "agent_offline"
    mock_agent_manager.action_container.assert_not_awaited()


def test_action_unreachable_agent_502(orchestrator_client, mock_agent_manager, bearer):
    _online(mock_agent_manager)
    mock_agent_manager.fetch_containers_stats.return_value = {
        "results": [_agent_stats(state="exited")]
    }
    mock_agent_manager.action_container.side_effect = RuntimeError("connection refused")

    resp = orchestrator_client.post(
        "/api/integration/v1/agents/A/containers/web/start", headers=bearer
    )
    assert resp.status_code == 502
    assert resp.json()["code"] == "agent_unreachable"


def test_action_agent_failure_502(orchestrator_client, mock_agent_manager, bearer):
    _online(mock_agent_manager)
    mock_agent_manager.fetch_containers_stats.return_value = {
        "results": [_agent_stats(state="exited")]
    }
    mock_agent_manager.action_container.return_value = {"success": False}

    resp = orchestrator_client.post(
        "/api/integration/v1/agents/A/containers/web/start", headers=bearer
    )
    assert resp.status_code == 502
    assert resp.json()["code"] == "action_failed"


def test_action_timeout_504(orchestrator_client, mock_agent_manager, bearer):
    _online(mock_agent_manager)
    mock_agent_manager.fetch_containers_stats.return_value = {
        "results": [_agent_stats(state="running")]
    }
    mock_agent_manager.action_container.side_effect = httpx.ReadTimeout("slow")

    resp = orchestrator_client.post(
        "/api/integration/v1/agents/A/containers/web/stop", headers=bearer
    )
    assert resp.status_code == 504
    assert resp.json() == {"error": "Action timed out", "code": "timeout"}


def test_action_precheck_timeout_504(orchestrator_client, mock_agent_manager, bearer):
    _online(mock_agent_manager)
    mock_agent_manager.fetch_containers_stats.side_effect = httpx.ReadTimeout("slow")

    resp = orchestrator_client.post(
        "/api/integration/v1/agents/A/containers/web/start", headers=bearer
    )
    assert resp.status_code == 504
    assert resp.json()["code"] == "timeout"


def test_action_requires_bearer(orchestrator_client, integration_key):
    resp = orchestrator_client.post(
        "/api/integration/v1/agents/A/containers/web/start"
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"
