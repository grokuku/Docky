"""Tests for the Docker Hub settings (orchestrator side).

Covers:
- ``GET /api/settings/dockerhub``: ``has_token`` present, token NEVER exposed.
- ``PUT /api/settings/dockerhub``: persistence, ASCII validation (400 FR),
  masked-token preservation, push to online agents (mocked agent manager).
- ``POST /api/settings/dockerhub/clear``: disable + push logout.
- ``AgentManager`` push mechanics: ``push_dockerhub_to_agent`` /
  ``push_dockerhub_all`` (online-only) and ``maybe_push_dockerhub_on_online``
  reconnection trigger with the anti-spam hash (one push per token, re-push
  on token change), plus the ``ping_agent`` offline→online transition hook.

No real network: the agent manager is either the ``mock_agent_manager``
fixture (settings endpoints) or a fresh ``AgentManager`` with a mocked
``_request`` (push mechanics), HTTP mocked with respx where needed.
"""

import hashlib

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

def _set_dockerhub(tmp_path, enabled=True, username="hubuser", token="dckr_pat_token123"):
    """Write the dockerhub section into <tmp_path>/settings.yaml."""
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
        return {"success": True, "message": "Connecté à Docker Hub"}

    monkeypatch.setattr(manager, "_request", _fake_request)
    return manager, requests


# ---------------------------------------------------------------------------
# GET /api/settings/dockerhub
# ---------------------------------------------------------------------------

def test_get_dockerhub_settings_defaults(auth_client):
    resp = auth_client.get("/api/settings/dockerhub")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["username"] == ""
    assert body["has_token"] is False


def test_get_dockerhub_settings_never_returns_token(auth_client, data_dir):
    _set_dockerhub(data_dir)
    resp = auth_client.get("/api/settings/dockerhub")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["username"] == "hubuser"
    assert body["has_token"] is True
    # The token value must not appear anywhere in the response.
    assert "dckr_pat_token123" not in resp.text
    assert "token" not in body


def test_get_dockerhub_settings_requires_auth(orchestrator_client):
    resp = orchestrator_client.get("/api/settings/dockerhub")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# PUT /api/settings/dockerhub
# ---------------------------------------------------------------------------

def test_put_dockerhub_enabled_then_get_returns_enabled(auth_client, data_dir, mock_agent_manager):
    """PUT with ``enabled: true`` → GET returns ``enabled: true``.

    Regression guard for the Settings badge: both payloads that drive the
    Docker Hub status pill must carry the confirmed state —

    1. the PUT response itself (persisted ``enabled``/``has_token``), which
       the frontend renders immediately after saving (no local guess), and
    2. the following GET snapshot (page reload), which must reflect the saved
       ``enabled`` state, not a stale ``False``.

    Combined ``enabled && has_token`` is what the pill maps to « Activé ».
    """
    mock_agent_manager.push_dockerhub_all.return_value = {"Test Agent": {"success": True}}

    put = auth_client.put(
        "/api/settings/dockerhub",
        json={"enabled": True, "username": "hubuser", "token": "dckr_pat_badge"},
    )
    assert put.status_code == 200
    put_body = put.json()
    assert put_body["success"] is True
    # Pill payload #1: the PUT response carries the persisted state.
    assert put_body["enabled"] is True
    assert put_body["has_token"] is True
    assert put_body["pushed"] == 1
    assert put_body["total"] == 1

    get = auth_client.get("/api/settings/dockerhub")
    assert get.status_code == 200
    body = get.json()
    # Pill payload #2: the GET snapshot agrees with the persisted state.
    assert body["enabled"] is True
    assert body["username"] == "hubuser"
    assert body["has_token"] is True


def test_put_dockerhub_response_carries_partial_push_results(auth_client, data_dir, mock_agent_manager):
    """The PUT response returns the per-agent push summary next to the state.

    The pill refines « Activé » into amber « Partiel » when the push could
    not be confirmed on every online agent (offline/errored) — it needs
    ``pushed``/``total``/``errors`` in the same response as the state.
    """
    mock_agent_manager.push_dockerhub_all.return_value = {
        "Up Agent": {"success": True},
        "Down Agent": {"success": False, "offline": True},
    }

    put = auth_client.put(
        "/api/settings/dockerhub",
        json={"enabled": True, "username": "hubuser", "token": "dckr_pat_part"},
    )
    assert put.status_code == 200
    body = put.json()
    assert body["enabled"] is True
    assert body["has_token"] is True
    assert body["pushed"] == 1
    assert body["total"] == 2
    assert body["errors"]["Down Agent"]


def test_get_dockerhub_incomplete_config_pill_payload(auth_client, data_dir):
    """``enabled`` without a stored token → the GET maps to amber « Incomplet ».

    A dockerhub section with ``enabled: true`` and no token cannot work; the
    GET payload (``enabled=true, has_token=false``) is what the frontend pill
    renders as « Incomplet » (amber) instead of a lying green « Activé ».
    """
    _set_dockerhub(data_dir, enabled=True, token="")
    resp = auth_client.get("/api/settings/dockerhub")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["has_token"] is False


def test_put_dockerhub_persists_and_pushes(auth_client, data_dir, mock_agent_manager):
    mock_agent_manager.push_dockerhub_all.return_value = {"Test Agent": {"success": True}}

    resp = auth_client.put(
        "/api/settings/dockerhub",
        json={"enabled": True, "username": "hubuser", "token": "dckr_pat_new"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["pushed"] == 1
    assert body["total"] == 1
    assert "Test Agent" in body["push"]
    mock_agent_manager.push_dockerhub_all.assert_awaited_once()

    stored = load_settings().get("dockerhub", {})
    assert stored == {"enabled": True, "username": "hubuser", "token": "dckr_pat_new"}
    # The push response must not echo the token.
    assert "dckr_pat_new" not in resp.text


def test_put_dockerhub_ascii_username_rejected_fr(auth_client, data_dir, mock_agent_manager):
    resp = auth_client.put(
        "/api/settings/dockerhub",
        json={"enabled": True, "username": "userù", "token": "tok"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == (
        "Le nom d'utilisateur Docker Hub ne doit contenir que des caractères ASCII"
    )
    # Nothing persisted, nothing pushed.
    assert "dockerhub" not in load_settings()
    mock_agent_manager.push_dockerhub_all.assert_not_awaited()


def test_put_dockerhub_ascii_token_rejected_fr(auth_client, data_dir, mock_agent_manager):
    resp = auth_client.put(
        "/api/settings/dockerhub",
        json={"enabled": True, "username": "hubuser", "token": "tokùn"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == (
        "Le token Docker Hub ne doit contenir que des caractères ASCII"
    )
    assert "dockerhub" not in load_settings()
    mock_agent_manager.push_dockerhub_all.assert_not_awaited()


def test_put_dockerhub_enabled_requires_credentials(auth_client, data_dir, mock_agent_manager):
    resp = auth_client.put(
        "/api/settings/dockerhub",
        json={"enabled": True, "username": "", "token": ""},
    )
    assert resp.status_code == 400
    assert "requis" in resp.json()["detail"]
    assert "dockerhub" not in load_settings()


def test_put_dockerhub_masked_token_preserved(auth_client, data_dir, mock_agent_manager):
    _set_dockerhub(data_dir, token="dckr_pat_original")
    mock_agent_manager.push_dockerhub_all.return_value = {"Test Agent": {"success": True}}

    resp = auth_client.put(
        "/api/settings/dockerhub",
        json={"enabled": True, "username": "hubuser", "token": "****"},
    )
    assert resp.status_code == 200
    assert load_settings()["dockerhub"]["token"] == "dckr_pat_original"


def test_put_dockerhub_disabled_without_credentials_ok(auth_client, data_dir, mock_agent_manager):
    """Disabling works without any credential (token stays stored)."""
    _set_dockerhub(data_dir, token="dckr_pat_kept")
    mock_agent_manager.push_dockerhub_all.return_value = {"Test Agent": {"success": True}}

    resp = auth_client.put(
        "/api/settings/dockerhub",
        json={"enabled": False, "username": "", "token": ""},
    )
    assert resp.status_code == 200
    stored = load_settings()["dockerhub"]
    assert stored["enabled"] is False
    assert stored["token"] == "dckr_pat_kept"


def test_put_dockerhub_requires_auth(orchestrator_client):
    resp = orchestrator_client.put("/api/settings/dockerhub", json={"enabled": False})
    assert resp.status_code == 401


def test_put_dockerhub_reports_offline_agents(auth_client, data_dir, mock_agent_manager):
    mock_agent_manager.push_dockerhub_all.return_value = {
        "Test Agent": {"success": True},
        "Down Agent": {"success": False, "offline": True},
    }

    resp = auth_client.put(
        "/api/settings/dockerhub",
        json={"enabled": True, "username": "hubuser", "token": "tok"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["pushed"] == 1
    assert body["total"] == 2
    assert body["errors"]["Down Agent"]


# ---------------------------------------------------------------------------
# POST /api/settings/dockerhub/clear
# ---------------------------------------------------------------------------

def test_clear_dockerhub_disables_and_pushes_logout(auth_client, data_dir, mock_agent_manager):
    _set_dockerhub(data_dir)
    mock_agent_manager.push_dockerhub_all.return_value = {"Test Agent": {"success": True}}

    resp = auth_client.post("/api/settings/dockerhub/clear")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["pushed"] == 1
    mock_agent_manager.push_dockerhub_all.assert_awaited_once()
    # Pill payload: the clear response carries the persisted (disabled)
    # state, which the frontend maps to grey/red « Désactivé ».
    assert body["enabled"] is False
    assert body["has_token"] is False
    assert body["username"] == ""

    stored = load_settings()["dockerhub"]
    assert stored == {"enabled": False, "username": "", "token": ""}


def test_clear_dockerhub_requires_auth(orchestrator_client):
    resp = orchestrator_client.post("/api/settings/dockerhub/clear")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# AgentManager push mechanics (fresh instance, mocked _request)
# ---------------------------------------------------------------------------

async def test_push_dockerhub_to_agent_sends_credentials(fresh_agent_manager, tmp_path):
    manager, requests = fresh_agent_manager
    _set_dockerhub(tmp_path, username="hubuser", token="dckr_pat_tok")

    result = await manager.push_dockerhub_to_agent("Test Agent")

    assert result["success"] is True
    assert requests == [
        ("Test Agent", "POST", "/agent/dockerhub/login",
         {"username": "hubuser", "token": "dckr_pat_tok", "enabled": True}),
    ]
    # The pushed token hash is recorded for the anti-spam.
    assert manager._dockerhub_pushed["Test Agent"] == _token_hash("dckr_pat_tok")


async def test_push_dockerhub_to_agent_incomplete_config(fresh_agent_manager, tmp_path):
    manager, requests = fresh_agent_manager
    _set_dockerhub(tmp_path, enabled=True, username="", token="")

    result = await manager.push_dockerhub_to_agent("Test Agent")

    assert result["success"] is False
    assert requests == []
    assert "Test Agent" not in manager._dockerhub_pushed


async def test_push_dockerhub_all_only_online_agents(fresh_agent_manager, tmp_path):
    manager, requests = fresh_agent_manager
    manager.agents = {
        "Online": {"url": "http://a", "api_key": "k", "status": "online"},
        "Offline": {"url": "http://b", "api_key": "k", "status": "offline"},
    }
    _set_dockerhub(tmp_path)

    results = await manager.push_dockerhub_all()

    assert results["Offline"] == {"success": False, "offline": True}
    assert results["Online"]["success"] is True
    assert [r[0] for r in requests] == ["Online"]


async def test_push_dockerhub_to_agent_agent_error(fresh_agent_manager, tmp_path):
    manager, _requests = fresh_agent_manager

    async def _failing(agent_name, method, path, timeout=30, **kwargs):
        raise RuntimeError("agent unreachable")

    manager._request = _failing
    _set_dockerhub(tmp_path)

    result = await manager.push_dockerhub_to_agent("Test Agent")

    assert result["success"] is False
    assert "agent unreachable" in result["error"]
    # A failed push must not record the hash (retry allowed at reconnection).
    assert "Test Agent" not in manager._dockerhub_pushed


# ---------------------------------------------------------------------------
# Reconnection trigger + anti-spam (maybe_push_dockerhub_on_online)
# ---------------------------------------------------------------------------

async def test_reconnect_pushes_once_then_skips(fresh_agent_manager, tmp_path):
    manager, requests = fresh_agent_manager
    manager.agents["Test Agent"]["status"] = "online"
    _set_dockerhub(tmp_path, token="dckr_pat_stable")

    # 1st reconnection: pushed (token never pushed to this agent).
    await manager.maybe_push_dockerhub_on_online("Test Agent")
    # 2nd reconnection with the same token: skipped (anti-spam hash).
    await manager.maybe_push_dockerhub_on_online("Test Agent")
    # 3rd reconnection after a token rotation: pushed again.
    _set_dockerhub(tmp_path, token="dckr_pat_rotated")
    await manager.maybe_push_dockerhub_on_online("Test Agent")

    assert len(requests) == 2
    assert requests[0][3]["token"] == "dckr_pat_stable"
    assert requests[1][3]["token"] == "dckr_pat_rotated"
    assert manager._dockerhub_pushed["Test Agent"] == _token_hash("dckr_pat_rotated")


async def test_reconnect_push_skipped_when_disabled(fresh_agent_manager, tmp_path):
    manager, requests = fresh_agent_manager
    manager.agents["Test Agent"]["status"] = "online"
    _set_dockerhub(tmp_path, enabled=False)

    await manager.maybe_push_dockerhub_on_online("Test Agent")

    assert requests == []


async def test_reconnect_push_skipped_when_offline(fresh_agent_manager, tmp_path):
    manager, requests = fresh_agent_manager
    manager.agents["Test Agent"]["status"] = "offline"
    _set_dockerhub(tmp_path)

    await manager.maybe_push_dockerhub_on_online("Test Agent")

    assert requests == []


async def test_reconnect_after_disable_pushes_logout_once(fresh_agent_manager, tmp_path):
    """An agent offline during 'clear' gets the logout push at reconnection."""
    manager, requests = fresh_agent_manager
    manager.agents["Test Agent"]["status"] = "online"
    _set_dockerhub(tmp_path)

    # Initial push (agent online, config enabled).
    await manager.maybe_push_dockerhub_on_online("Test Agent")
    # Config disabled while the agent is "offline" (orchestrator clear).
    _set_dockerhub(tmp_path, enabled=False, username="", token="")
    # Reconnection: the previously-pushed agent receives the logout once.
    await manager.maybe_push_dockerhub_on_online("Test Agent")
    # A further reconnection after the logout push: nothing more.
    await manager.maybe_push_dockerhub_on_online("Test Agent")

    assert len(requests) == 2
    assert requests[0][3]["enabled"] is True
    assert requests[1][3] == {"username": "", "token": "", "enabled": False}
    assert "Test Agent" not in manager._dockerhub_pushed


async def test_reconnect_push_skipped_when_already_inflight(fresh_agent_manager, tmp_path):
    """A concurrent push (inflight guard) is not duplicated."""
    manager, requests = fresh_agent_manager
    manager.agents["Test Agent"]["status"] = "online"
    _set_dockerhub(tmp_path)

    manager._dockerhub_push_inflight.add("Test Agent")
    await manager.maybe_push_dockerhub_on_online("Test Agent")

    assert requests == []


# ---------------------------------------------------------------------------
# ping_agent transition → push hook
# ---------------------------------------------------------------------------

async def test_ping_agent_transition_triggers_push(respx_mock, monkeypatch, tmp_path):
    """An agent going offline→online via ping gets the Docker Hub push."""
    monkeypatch.setenv("DOCKY_DATA_DIR", str(tmp_path))
    make_settings(tmp_path)
    _set_dockerhub(tmp_path, token="dckr_pat_ping")

    from app.agent_manager.client import AgentManager

    manager = AgentManager()
    pushed = []

    async def _fake_maybe(name):
        pushed.append(name)

    monkeypatch.setattr(manager, "maybe_push_dockerhub_on_online", _fake_maybe)
    respx_mock.get("http://agent:8080/agent/health").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )

    # First ping: unknown → online → push hook fired.
    assert await manager.ping_agent("Test Agent") is True
    assert pushed == ["Test Agent"]
    # Second ping: already online → no new push.
    assert await manager.ping_agent("Test Agent") is True
    assert pushed == ["Test Agent"]


# ---------------------------------------------------------------------------
# Config defaults (ensure_config_files)
# ---------------------------------------------------------------------------

def test_default_settings_contain_dockerhub_section(tmp_path, monkeypatch):
    """ensure_config_files writes a disabled dockerhub section by default."""
    import app.config as config

    monkeypatch.setattr(config, "get_data_dir", lambda: tmp_path)
    config.ensure_config_files()
    settings = config.load_settings()
    assert settings["dockerhub"] == {"enabled": False, "username": "", "token": ""}