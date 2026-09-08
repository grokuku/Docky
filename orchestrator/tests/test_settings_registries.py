"""Tests for the multi-registry settings (orchestrator side, LOT 2).

Covers:
- one-time migration ``dockerhub`` → ``registries`` (docker.io entry);
- ``GET /api/settings/registries``: configured registries (``has_token``,
  ``push_status`` per agent), aggregated discovered registries (TTL ~5 min,
  ``refresh=1``), tailscale placeholder; token NEVER exposed;
- ``PUT /api/settings/registries``: upsert, ASCII + valid-host validation
  (400 FR), masked-token preservation, push to online agents (mocked);
- ``DELETE /api/settings/registries/{url}``: removal + logout push;
- ``AgentManager`` push mechanics: ``push_registry_all`` (online-only),
  ``maybe_push_registries_on_online`` reconnection trigger with the anti-spam
  hash per registre×agent, and the aggregated scan cache TTL;
- tailscale placeholder persistence (no effect).

No real network: the agent manager is either the ``mock_agent_manager``
fixture (settings endpoints) or a fresh ``AgentManager`` with a mocked
``_request`` (push mechanics).
"""

import hashlib
import time

import httpx
import pytest
import respx
import yaml

from app.agent_manager.client import AgentManager
from app.config import load_settings, save_settings
from orchestrator.tests._helpers import make_settings


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _set_registries(tmp_path, registries):
    """Write the ``registries`` list into <tmp_path>/settings.yaml."""
    settings_path = tmp_path / "settings.yaml"
    settings = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
    settings["registries"] = registries
    settings_path.write_text(
        yaml.safe_dump(settings, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    return settings["registries"]


def _set_dockerhub(tmp_path, enabled=True, username="hubuser", token="dckr_pat_token123"):
    """Write the legacy dockerhub section into <tmp_path>/settings.yaml."""
    settings_path = tmp_path / "settings.yaml"
    settings = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
    settings["dockerhub"] = {"enabled": enabled, "username": username, "token": token}
    settings_path.write_text(
        yaml.safe_dump(settings, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    return settings["dockerhub"]


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@pytest.fixture
def fresh_agent_manager(monkeypatch, tmp_path):
    """A fresh ``AgentManager`` bound to ``tmp_path`` with a mocked ``_request``.

    Returns ``(manager, requests)`` where ``requests`` records
    ``(agent_name, method, path, json_body)`` for every call.
    """
    monkeypatch.setenv("DOCKY_DATA_DIR", str(tmp_path))
    make_settings(tmp_path)
    from app.agent_manager.client import AgentManager

    manager = AgentManager()
    requests = []

    async def _fake_request(agent_name, method, path, timeout=30, **kwargs):
        requests.append((agent_name, method, path, kwargs.get("json")))
        return {"success": True, "message": "Connecté"}

    monkeypatch.setattr(manager, "_request", _fake_request)
    return manager, requests


# ---------------------------------------------------------------------------
# One-time migration dockerhub → registries
# ---------------------------------------------------------------------------

def test_migrate_dockerhub_to_registries_creates_dockerio_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKY_DATA_DIR", str(tmp_path))
    make_settings(tmp_path)
    _set_dockerhub(tmp_path, enabled=True, username="hubuser", token="dckr_pat_mig")

    import app.config as config

    assert config.migrate_dockerhub_to_registries() is True

    settings = config.load_settings()
    assert "dockerhub" not in settings
    assert settings["registries"] == [{
        "url": "docker.io",
        "username": "hubuser",
        "token": "dckr_pat_mig",
        "tailscale": False,
        "tailscale_host": "",
    }]


def test_migrate_dockerhub_disabled_removes_section_without_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKY_DATA_DIR", str(tmp_path))
    make_settings(tmp_path)
    _set_dockerhub(tmp_path, enabled=False, username="", token="")

    import app.config as config

    assert config.migrate_dockerhub_to_registries() is True
    settings = config.load_settings()
    assert "dockerhub" not in settings
    assert settings["registries"] == []


def test_migrate_dockerhub_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKY_DATA_DIR", str(tmp_path))
    make_settings(tmp_path)
    _set_dockerhub(tmp_path, token="dckr_pat_mig")

    import app.config as config

    config.migrate_dockerhub_to_registries()
    # Second call: no dockerhub section anymore → no-op, no duplicate entry.
    assert config.migrate_dockerhub_to_registries() is False
    settings = config.load_settings()
    assert len(settings["registries"]) == 1


def test_migrate_dockerhub_noop_without_section(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKY_DATA_DIR", str(tmp_path))
    make_settings(tmp_path)

    import app.config as config

    assert config.migrate_dockerhub_to_registries() is False


# ---------------------------------------------------------------------------
# GET /api/settings/registries
# ---------------------------------------------------------------------------

def test_get_registries_defaults(auth_client, mock_agent_manager):
    mock_agent_manager.get_aggregated_discovered_registries.return_value = {
        "discovered": [], "stored": [],
    }
    resp = auth_client.get("/api/settings/registries")
    assert resp.status_code == 200
    body = resp.json()
    assert body["registries"] == []
    assert body["discovered"] == []
    assert body["tailscale"] == {"enabled": False, "host": ""}


def test_get_registries_returns_configured_and_discovered(auth_client, data_dir, mock_agent_manager):
    _set_registries(data_dir, [{
        "url": "ghcr.io",
        "username": "ghuser",
        "token": "ghcr_pat",
        "tailscale": False,
        "tailscale_host": "",
    }])
    mock_agent_manager.get_aggregated_discovered_registries.return_value = {
        "discovered": ["docker.io", "ghcr.io", "lscr.io"], "stored": ["ghcr.io"],
    }
    mock_agent_manager.registry_push_status.return_value = {"Test Agent": "ok"}

    resp = auth_client.get("/api/settings/registries")
    assert resp.status_code == 200
    body = resp.json()
    assert body["registries"][0]["url"] == "ghcr.io"
    assert body["registries"][0]["username"] == "ghuser"
    assert body["registries"][0]["has_token"] is True
    assert body["registries"][0]["push_status"] == {"Test Agent": "ok"}
    assert body["discovered"] == ["docker.io", "ghcr.io", "lscr.io"]
    # The token value must not appear anywhere in the response.
    assert "ghcr_pat" not in resp.text
    assert "token" not in body["registries"][0]


def test_get_registries_requires_auth(orchestrator_client):
    resp = orchestrator_client.get("/api/settings/registries")
    assert resp.status_code == 401


def test_get_registries_passes_refresh_flag(auth_client, mock_agent_manager):
    mock_agent_manager.get_aggregated_discovered_registries.return_value = {
        "discovered": [], "stored": [],
    }
    auth_client.get("/api/settings/registries?refresh=1")
    mock_agent_manager.get_aggregated_discovered_registries.assert_awaited_once_with(refresh=True)


# ---------------------------------------------------------------------------
# PUT /api/settings/registries
# ---------------------------------------------------------------------------

def test_put_registry_persists_and_pushes(auth_client, data_dir, mock_agent_manager):
    mock_agent_manager.push_registry_all.return_value = {"Test Agent": {"success": True}}

    resp = auth_client.put(
        "/api/settings/registries",
        json={"url": "ghcr.io", "username": "ghuser", "token": "ghcr_pat_new"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["registry"]["url"] == "ghcr.io"
    assert body["registry"]["has_token"] is True
    assert body["pushed"] == 1
    assert body["total"] == 1
    mock_agent_manager.push_registry_all.assert_awaited_once_with("ghcr.io")

    stored = load_settings()["registries"]
    assert stored == [{
        "url": "ghcr.io", "username": "ghuser", "token": "ghcr_pat_new",
        "tailscale": False, "tailscale_host": "",
    }]
    assert "ghcr_pat_new" not in resp.text


def test_put_registry_normalizes_dockerhub_alias(auth_client, data_dir, mock_agent_manager):
    mock_agent_manager.push_registry_all.return_value = {}
    resp = auth_client.put(
        "/api/settings/registries",
        json={"url": "https://index.docker.io/v1/", "username": "u", "token": "t"},
    )
    assert resp.status_code == 200
    assert resp.json()["registry"]["url"] == "docker.io"
    assert load_settings()["registries"][0]["url"] == "docker.io"


def test_put_registry_ascii_username_rejected_fr(auth_client, data_dir, mock_agent_manager):
    resp = auth_client.put(
        "/api/settings/registries",
        json={"url": "ghcr.io", "username": "userù", "token": "tok"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == (
        "Le nom d'utilisateur du registre ne doit contenir que des caractères ASCII"
    )
    assert "registries" not in load_settings()
    mock_agent_manager.push_registry_all.assert_not_awaited()


def test_put_registry_ascii_token_rejected_fr(auth_client, data_dir, mock_agent_manager):
    resp = auth_client.put(
        "/api/settings/registries",
        json={"url": "ghcr.io", "username": "ghuser", "token": "tokùn"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == (
        "Le token du registre ne doit contenir que des caractères ASCII"
    )
    assert "registries" not in load_settings()


def test_put_registry_invalid_host_rejected_fr(auth_client, data_dir, mock_agent_manager):
    resp = auth_client.put(
        "/api/settings/registries",
        json={"url": "not a host/with path", "username": "u", "token": "t"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == (
        "L'URL du registre doit être un hôte valide (ex. docker.io, ghcr.io)"
    )
    assert "registries" not in load_settings()


def test_put_registry_masked_token_preserved(auth_client, data_dir, mock_agent_manager):
    _set_registries(data_dir, [{
        "url": "ghcr.io", "username": "ghuser", "token": "ghcr_pat_original",
        "tailscale": False, "tailscale_host": "",
    }])
    mock_agent_manager.push_registry_all.return_value = {"Test Agent": {"success": True}}

    resp = auth_client.put(
        "/api/settings/registries",
        json={"url": "ghcr.io", "username": "ghuser", "token": "****"},
    )
    assert resp.status_code == 200
    assert load_settings()["registries"][0]["token"] == "ghcr_pat_original"


def test_put_registry_requires_auth(orchestrator_client):
    resp = orchestrator_client.put("/api/settings/registries", json={"url": "ghcr.io"})
    assert resp.status_code == 401


def test_put_registry_reports_offline_agents(auth_client, data_dir, mock_agent_manager):
    mock_agent_manager.push_registry_all.return_value = {
        "Test Agent": {"success": True},
        "Down Agent": {"success": False, "offline": True},
    }
    resp = auth_client.put(
        "/api/settings/registries",
        json={"url": "ghcr.io", "username": "u", "token": "t"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["pushed"] == 1
    assert body["total"] == 2
    assert body["errors"]["Down Agent"]


# ---------------------------------------------------------------------------
# DELETE /api/settings/registries/{url}
# ---------------------------------------------------------------------------

def test_delete_registry_removes_and_logs_out(auth_client, data_dir, mock_agent_manager):
    _set_registries(data_dir, [{
        "url": "ghcr.io", "username": "ghuser", "token": "ghcr_pat",
        "tailscale": False, "tailscale_host": "",
    }])
    mock_agent_manager.logout_registry_all.return_value = {"Test Agent": {"success": True}}

    resp = auth_client.delete("/api/settings/registries/ghcr.io")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["pushed"] == 1
    mock_agent_manager.logout_registry_all.assert_awaited_once_with("ghcr.io")
    assert load_settings()["registries"] == []


def test_delete_registry_not_found(auth_client, data_dir, mock_agent_manager):
    resp = auth_client.delete("/api/settings/registries/ghcr.io")
    assert resp.status_code == 404
    assert "non trouvé" in resp.json()["detail"]
    mock_agent_manager.logout_registry_all.assert_not_awaited()


def test_delete_registry_requires_auth(orchestrator_client):
    resp = orchestrator_client.delete("/api/settings/registries/ghcr.io")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Tailscale placeholder (persisted, no effect)
# ---------------------------------------------------------------------------

def test_put_tailscale_placeholder_persists(auth_client, data_dir, mock_agent_manager):
    resp = auth_client.put(
        "/api/settings/registries",
        json={"tailscale": {"enabled": True, "host": "tailnet.example.com"}},
    )
    assert resp.status_code == 200
    assert resp.json()["tailscale"] == {"enabled": True, "host": "tailnet.example.com"}
    assert load_settings()["tailscale"] == {"enabled": True, "host": "tailnet.example.com"}
    # No registry push for a placeholder-only update.
    mock_agent_manager.push_registry_all.assert_not_awaited()


def test_get_tailscale_placeholder_roundtrip(auth_client, data_dir, mock_agent_manager):
    settings = load_settings()
    settings["tailscale"] = {"enabled": True, "host": "tailnet.example.com"}
    save_settings(settings)
    mock_agent_manager.get_aggregated_discovered_registries.return_value = {
        "discovered": [], "stored": [],
    }
    resp = auth_client.get("/api/settings/registries")
    assert resp.status_code == 200
    assert resp.json()["tailscale"] == {"enabled": True, "host": "tailnet.example.com"}


# ---------------------------------------------------------------------------
# AgentManager push mechanics (fresh instance, mocked _request)
# ---------------------------------------------------------------------------

async def test_push_registry_to_agent_sends_credentials(fresh_agent_manager, tmp_path):
    manager, requests = fresh_agent_manager
    _set_registries(tmp_path, [{
        "url": "ghcr.io", "username": "ghuser", "token": "ghcr_pat_tok",
        "tailscale": False, "tailscale_host": "",
    }])

    result = await manager.push_registry_to_agent("Test Agent", "ghcr.io")

    assert result["success"] is True
    assert requests == [
        ("Test Agent", "POST", "/agent/registry/login",
         {"registry": "ghcr.io", "username": "ghuser", "token": "ghcr_pat_tok"}),
    ]
    assert manager._registry_pushed["ghcr.io"]["Test Agent"] == _token_hash("ghcr_pat_tok")
    assert manager._registry_push_status["ghcr.io"]["Test Agent"] == "ok"


async def test_push_registry_to_agent_incomplete_config(fresh_agent_manager, tmp_path):
    manager, requests = fresh_agent_manager
    _set_registries(tmp_path, [{
        "url": "ghcr.io", "username": "", "token": "",
        "tailscale": False, "tailscale_host": "",
    }])

    result = await manager.push_registry_to_agent("Test Agent", "ghcr.io")

    assert result["success"] is False
    assert requests == []
    assert "Test Agent" not in manager._registry_pushed.get("ghcr.io", {})


async def test_push_registry_all_only_online_agents(fresh_agent_manager, tmp_path):
    manager, requests = fresh_agent_manager
    manager.agents = {
        "Online": {"url": "http://a", "api_key": "k", "status": "online"},
        "Offline": {"url": "http://b", "api_key": "k", "status": "offline"},
    }
    _set_registries(tmp_path, [{
        "url": "ghcr.io", "username": "ghuser", "token": "ghcr_pat",
        "tailscale": False, "tailscale_host": "",
    }])

    results = await manager.push_registry_all("ghcr.io")

    assert results["Offline"] == {"success": False, "offline": True}
    assert results["Online"]["success"] is True
    assert [r[0] for r in requests] == ["Online"]
    assert manager._registry_push_status["ghcr.io"]["Offline"] == "offline"


async def test_push_registry_all_skips_without_token(fresh_agent_manager, tmp_path):
    manager, requests = fresh_agent_manager
    manager.agents = {"Online": {"url": "http://a", "api_key": "k", "status": "online"}}
    _set_registries(tmp_path, [{
        "url": "ghcr.io", "username": "ghuser", "token": "",
        "tailscale": False, "tailscale_host": "",
    }])

    results = await manager.push_registry_all("ghcr.io")

    assert results == {}
    assert requests == []


async def test_push_registry_to_agent_agent_error(fresh_agent_manager, tmp_path):
    manager, _requests = fresh_agent_manager

    async def _failing(agent_name, method, path, timeout=30, **kwargs):
        raise RuntimeError("agent unreachable")

    manager._request = _failing
    _set_registries(tmp_path, [{
        "url": "ghcr.io", "username": "ghuser", "token": "ghcr_pat",
        "tailscale": False, "tailscale_host": "",
    }])

    result = await manager.push_registry_to_agent("Test Agent", "ghcr.io")

    assert result["success"] is False
    assert "agent unreachable" in result["error"]
    assert "Test Agent" not in manager._registry_pushed.get("ghcr.io", {})
    assert manager._registry_push_status["ghcr.io"]["Test Agent"] == "error"


# ---------------------------------------------------------------------------
# Reconnection trigger + anti-spam per registre×agent
# ---------------------------------------------------------------------------

async def test_reconnect_pushes_each_registry_once_then_skips(fresh_agent_manager, tmp_path):
    manager, requests = fresh_agent_manager
    manager.agents["Test Agent"]["status"] = "online"
    _set_registries(tmp_path, [
        {"url": "ghcr.io", "username": "ghuser", "token": "ghcr_pat_stable",
         "tailscale": False, "tailscale_host": ""},
        {"url": "docker.io", "username": "hubuser", "token": "dckr_pat_stable",
         "tailscale": False, "tailscale_host": ""},
    ])

    # 1st reconnection: both registries pushed.
    await manager.maybe_push_registries_on_online("Test Agent")
    # 2nd reconnection with the same tokens: skipped (anti-spam per registre×agent).
    await manager.maybe_push_registries_on_online("Test Agent")
    # 3rd reconnection after a token rotation on ghcr.io only: ghcr re-pushed.
    _set_registries(tmp_path, [
        {"url": "ghcr.io", "username": "ghuser", "token": "ghcr_pat_rotated",
         "tailscale": False, "tailscale_host": ""},
        {"url": "docker.io", "username": "hubuser", "token": "dckr_pat_stable",
         "tailscale": False, "tailscale_host": ""},
    ])
    await manager.maybe_push_registries_on_online("Test Agent")

    assert len(requests) == 3
    assert requests[0][3]["registry"] == "ghcr.io"
    assert requests[1][3]["registry"] == "docker.io"
    assert requests[2][3]["registry"] == "ghcr.io"
    assert requests[2][3]["token"] == "ghcr_pat_rotated"
    assert manager._registry_pushed["ghcr.io"]["Test Agent"] == _token_hash("ghcr_pat_rotated")
    assert manager._registry_pushed["docker.io"]["Test Agent"] == _token_hash("dckr_pat_stable")


async def test_reconnect_push_skipped_when_offline(fresh_agent_manager, tmp_path):
    manager, requests = fresh_agent_manager
    manager.agents["Test Agent"]["status"] = "offline"
    _set_registries(tmp_path, [{
        "url": "ghcr.io", "username": "ghuser", "token": "ghcr_pat",
        "tailscale": False, "tailscale_host": "",
    }])

    await manager.maybe_push_registries_on_online("Test Agent")

    assert requests == []


async def test_reconnect_after_removal_pushes_logout_once(fresh_agent_manager, tmp_path):
    """A registry removed while the agent was offline gets the logout at reconnection."""
    manager, requests = fresh_agent_manager
    manager.agents["Test Agent"]["status"] = "online"
    _set_registries(tmp_path, [{
        "url": "ghcr.io", "username": "ghuser", "token": "ghcr_pat",
        "tailscale": False, "tailscale_host": "",
    }])

    # Initial push (agent online, registry configured).
    await manager.maybe_push_registries_on_online("Test Agent")
    # Agent goes offline, then the registry is removed (DELETE) while offline.
    manager.agents["Test Agent"]["status"] = "offline"
    _set_registries(tmp_path, [])
    await manager.logout_registry_all("ghcr.io")  # offline → pending logout
    # Reconnection: the previously-pushed registry receives the logout once.
    manager.agents["Test Agent"]["status"] = "online"
    await manager.maybe_push_registries_on_online("Test Agent")
    # A further reconnection after the logout push: nothing more.
    await manager.maybe_push_registries_on_online("Test Agent")

    assert len(requests) == 2
    assert requests[0][3]["registry"] == "ghcr.io"
    assert requests[1] == ("Test Agent", "POST", "/agent/registry/logout", {"registry": "ghcr.io"})
    assert "Test Agent" not in manager._registry_pushed.get("ghcr.io", {})


async def test_reconnect_push_skipped_when_already_inflight(fresh_agent_manager, tmp_path):
    manager, requests = fresh_agent_manager
    manager.agents["Test Agent"]["status"] = "online"
    _set_registries(tmp_path, [{
        "url": "ghcr.io", "username": "ghuser", "token": "ghcr_pat",
        "tailscale": False, "tailscale_host": "",
    }])

    manager._registry_push_inflight.add(("ghcr.io", "Test Agent"))
    await manager.maybe_push_registries_on_online("Test Agent")

    assert requests == []


# ---------------------------------------------------------------------------
# Aggregated scan cache (TTL ~5 min)
# ---------------------------------------------------------------------------

async def test_scan_cache_ttl_and_refresh(fresh_agent_manager, tmp_path):
    manager, requests = fresh_agent_manager
    manager.agents = {"Online": {"url": "http://a", "api_key": "k", "status": "online"}}

    # First scan: queries the agent and caches.
    data = await manager.get_aggregated_discovered_registries()
    assert data == {"discovered": [], "stored": []}
    assert len(requests) == 1

    # Second scan within TTL: served from cache, no new request.
    data = await manager.get_aggregated_discovered_registries()
    assert len(requests) == 1

    # refresh=1 forces a new scan.
    data = await manager.get_aggregated_discovered_registries(refresh=True)
    assert len(requests) == 2


async def test_scan_cache_expires_after_ttl(fresh_agent_manager, tmp_path, monkeypatch):
    manager, requests = fresh_agent_manager
    manager.agents = {"Online": {"url": "http://a", "api_key": "k", "status": "online"}}

    await manager.get_aggregated_discovered_registries()
    assert len(requests) == 1

    # Simulate the TTL elapsing.
    manager._registries_scan_cache["timestamp"] = time.time() - 301
    await manager.get_aggregated_discovered_registries()
    assert len(requests) == 2


async def test_scan_aggregates_across_agents(fresh_agent_manager, tmp_path):
    manager, requests = fresh_agent_manager
    manager.agents = {
        "A": {"url": "http://a", "api_key": "k", "status": "online"},
        "B": {"url": "http://b", "api_key": "k", "status": "online"},
    }

    async def _fake_request(agent_name, method, path, timeout=30, **kwargs):
        if agent_name == "A":
            return {"discovered": ["ghcr.io", "docker.io"], "stored": ["ghcr.io"]}
        return {"discovered": ["lscr.io", "ghcr.io"], "stored": []}

    manager._request = _fake_request

    data = await manager.get_aggregated_discovered_registries()
    assert data == {"discovered": ["docker.io", "ghcr.io", "lscr.io"], "stored": ["ghcr.io"]}
