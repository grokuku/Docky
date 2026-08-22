"""Tests for the orchestrator API routes (``app.routes.api``).

Every request goes through a ``TestClient`` with the startup background
refresh neutralised; ``agent_manager`` and ``LLMClient`` are mocked so no real
network / LLM / Docker call ever happens.
"""

import yaml
import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock


# ---------------------------------------------------------------------------
# Authentication guard
# ---------------------------------------------------------------------------

def test_api_requires_auth(orchestrator_client):
    resp = orchestrator_client.get("/api/agents")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Unauthorized"


def test_api_invalid_cookie_rejected(orchestrator_client):
    orchestrator_client.cookies.set("docky_token", "garbage-token")
    resp = orchestrator_client.get("/api/agents")
    assert resp.status_code == 401


def test_api_valid_cookie_accepted(auth_client, mock_agent_manager):
    mock_agent_manager.ping_all = AsyncMock(return_value=None)
    mock_agent_manager.list_agents.return_value = [
        {"name": "Test Agent", "url": "http://agent:8080", "status": "online"}
    ]
    resp = auth_client.get("/api/agents")
    assert resp.status_code == 200
    assert resp.json()[0]["name"] == "Test Agent"


# ---------------------------------------------------------------------------
# _resolve_agent
# ---------------------------------------------------------------------------

def test_resolve_agent_absent_400(auth_client, mock_agent_manager):
    resp = auth_client.get("/api/containers", params={"agent": ""})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "agent parameter required"


def test_resolve_agent_unknown_404(auth_client, mock_agent_manager):
    resp = auth_client.get("/api/containers", params={"agent": "ghost"})
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


def test_resolve_agent_offline_503(auth_client, mock_agent_manager):
    mock_agent_manager.agents["Test Agent"]["status"] = "offline"
    resp = auth_client.get("/api/containers", params={"agent": "Test Agent"})
    assert resp.status_code == 503
    assert "is offline" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# _check_agent_error
# ---------------------------------------------------------------------------

def test_check_agent_error_unreachable_502():
    from app.routes import api as api_mod

    resp = api_mod._check_agent_error(
        {"success": False, "error": "agent unreachable", "unreachable": True}
    )
    assert resp is not None
    assert resp.status_code == 502


def test_check_agent_error_business_error_500():
    import json

    from app.routes import api as api_mod

    resp = api_mod._check_agent_error({"success": False, "error": "compose error"})
    assert resp is not None
    assert resp.status_code == 500
    body = json.loads(resp.body.decode("utf-8"))
    assert body["detail"] == "compose error"
    assert body["error"] == "compose error"
    assert body["success"] is False


def test_check_agent_error_success_returns_none():
    from app.routes import api as api_mod

    assert api_mod._check_agent_error({"success": True}) is None
    assert api_mod._check_agent_error(None) is None
    assert api_mod._check_agent_error({"error": "no success field"}) is None


# ---------------------------------------------------------------------------
# Agents / version
# ---------------------------------------------------------------------------

def test_get_agents_list(auth_client, mock_agent_manager):
    mock_agent_manager.ping_all = AsyncMock(return_value=None)
    mock_agent_manager.list_agents.return_value = [
        {"name": "Test Agent", "url": "http://agent:8080", "status": "online"}
    ]
    resp = auth_client.get("/api/agents")
    assert resp.status_code == 200
    assert resp.json()[0]["name"] == "Test Agent"


def test_get_version(auth_client):
    resp = auth_client.get("/api/version")
    assert resp.status_code == 200
    assert "version" in resp.json()
    assert resp.json()["version"] == "0.0.4"


# ---------------------------------------------------------------------------
# /api/settings/llm
# ---------------------------------------------------------------------------

def test_get_llm_settings(auth_client):
    resp = auth_client.get("/api/settings/llm")
    assert resp.status_code == 200
    body = resp.json()
    assert "endpoint" in body
    assert "api_key" in body
    assert "model" in body


def test_put_llm_settings_persists_and_masks(auth_client, data_dir):
    resp = auth_client.put(
        "/api/settings/llm",
        json={"endpoint": "http://llm.test/v1", "api_key": "sk-super-secret", "model": "gpt"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"success": True}

    settings = yaml.safe_load((data_dir / "settings.yaml").read_text(encoding="utf-8"))
    assert settings["llm"] == {
        "endpoint": "http://llm.test/v1",
        "api_key": "sk-super-secret",
        "model": "gpt",
    }

    # GET masks the key (last 4 chars only).
    resp = auth_client.get("/api/settings/llm")
    assert resp.json()["api_key"] == "****cret"


def test_put_llm_settings_masked_key_preserved(auth_client, data_dir):
    auth_client.put(
        "/api/settings/llm",
        json={"endpoint": "http://llm.test/v1", "api_key": "sk-original", "model": "m"},
    )
    # A masked value must not overwrite the stored key.
    resp = auth_client.put(
        "/api/settings/llm",
        json={"endpoint": "http://llm.test/v1", "api_key": "****inal", "model": "m2"},
    )
    assert resp.status_code == 200
    settings = yaml.safe_load((data_dir / "settings.yaml").read_text(encoding="utf-8"))
    assert settings["llm"]["api_key"] == "sk-original"
    assert settings["llm"]["model"] == "m2"


def test_scan_llm_models(respx_mock, auth_client):
    respx_mock.get("http://llm.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "model-a"}, {"id": "model-b"}]})
    )
    resp = auth_client.post(
        "/api/settings/llm/models", json={"endpoint": "http://llm.test/v1", "api_key": ""}
    )
    assert resp.status_code == 200
    assert resp.json() == {"success": True, "models": ["model-a", "model-b"]}


def test_scan_llm_models_requires_endpoint(auth_client):
    resp = auth_client.post("/api/settings/llm/models", json={"endpoint": ""})
    assert resp.status_code == 400


def test_test_llm_success(auth_client, mock_llm_client):
    mock_llm_client.is_configured.return_value = True
    mock_llm_client.chat = AsyncMock(
        return_value={"choices": [{"message": {"role": "assistant", "content": "hi"}}]}
    )
    resp = auth_client.post("/api/settings/llm/test", json={})
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_test_llm_not_configured(auth_client, mock_llm_client):
    mock_llm_client.is_configured.return_value = False
    resp = auth_client.post("/api/settings/llm/test", json={})
    assert resp.status_code == 400
    assert resp.json()["success"] is False


# ---------------------------------------------------------------------------
# /api/settings/agents CRUD
# ---------------------------------------------------------------------------

def test_get_settings_agents(auth_client, mock_agent_manager):
    resp = auth_client.get("/api/settings/agents")
    assert resp.status_code == 200
    names = [a["name"] for a in resp.json()]
    assert "Test Agent" in names


def test_add_settings_agent(auth_client, mock_agent_manager, data_dir):
    resp = auth_client.post(
        "/api/settings/agents",
        json={"name": "Agent 2", "url": "http://agent2:8080", "api_key": "k2"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"success": True}
    settings = yaml.safe_load((data_dir / "settings.yaml").read_text(encoding="utf-8"))
    assert any(a["name"] == "Agent 2" for a in settings["agents"])
    # reload() must be invoked on the manager after persistence.
    mock_agent_manager.reload.assert_called_once()


def test_add_settings_agent_duplicate_conflict(auth_client, mock_agent_manager):
    resp = auth_client.post(
        "/api/settings/agents",
        json={"name": "Test Agent", "url": "http://x:8080", "api_key": "k"},
    )
    assert resp.status_code == 409


def test_add_settings_agent_missing_fields_400(auth_client, mock_agent_manager):
    resp = auth_client.post("/api/settings/agents", json={"name": "", "url": ""})
    assert resp.status_code == 400


def test_update_settings_agent(auth_client, mock_agent_manager, data_dir):
    resp = auth_client.put(
        "/api/settings/agents/Test Agent",
        json={"url": "http://agent-updated:8080"},
    )
    assert resp.status_code == 200
    settings = yaml.safe_load((data_dir / "settings.yaml").read_text(encoding="utf-8"))
    agent = next(a for a in settings["agents"] if a["name"] == "Test Agent")
    assert agent["url"] == "http://agent-updated:8080"


def test_update_settings_agent_not_found(auth_client, mock_agent_manager):
    resp = auth_client.put("/api/settings/agents/ghost", json={"url": "http://x:8080"})
    assert resp.status_code == 404


def test_delete_settings_agent(auth_client, mock_agent_manager, data_dir):
    # Add a second agent first.
    auth_client.post(
        "/api/settings/agents",
        json={"name": "Agent 2", "url": "http://agent2:8080", "api_key": "k2"},
    )
    resp = auth_client.delete("/api/settings/agents/Agent 2")
    assert resp.status_code == 200
    settings = yaml.safe_load((data_dir / "settings.yaml").read_text(encoding="utf-8"))
    assert all(a["name"] != "Agent 2" for a in settings["agents"])


def test_delete_settings_agent_not_found(auth_client, mock_agent_manager):
    resp = auth_client.delete("/api/settings/agents/ghost")
    assert resp.status_code == 404


def test_test_settings_agent(auth_client, mock_agent_manager):
    mock_agent_manager.ping_agent.return_value = True
    resp = auth_client.post("/api/settings/agents/Test Agent/test")
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert resp.json()["status"] == "online"


# ---------------------------------------------------------------------------
# /api/change-password
# ---------------------------------------------------------------------------

def test_change_password_success(auth_client, data_dir):
    resp = auth_client.put(
        "/api/settings/password",
        json={"current_password": "docky123", "new_password": "nouveau-pass"},
    )
    assert resp.status_code == 200
    users = yaml.safe_load((data_dir / "users.yaml").read_text(encoding="utf-8"))
    admin = next(u for u in users["users"] if u["username"] == "admin")
    import bcrypt

    assert bcrypt.checkpw(b"nouveau-pass", admin["password_hash"].encode("utf-8"))


def test_change_password_wrong_current(auth_client):
    resp = auth_client.put(
        "/api/settings/password",
        json={"current_password": "wrong", "new_password": "nouveau-pass"},
    )
    assert resp.status_code == 400
    assert "Current password is incorrect" in resp.json()["detail"]


def test_change_password_too_short(auth_client):
    resp = auth_client.put(
        "/api/settings/password",
        json={"current_password": "docky123", "new_password": "abc"},
    )
    assert resp.status_code == 400
    assert "too short" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Proxy endpoints (containers / stacks / ports)
# ---------------------------------------------------------------------------

def test_proxy_containers(auth_client, mock_agent_manager):
    mock_agent_manager.get_containers.return_value = [{"Id": "c1", "name": "web"}]
    resp = auth_client.get("/api/containers", params={"agent": "Test Agent"})
    assert resp.status_code == 200
    assert resp.json() == [{"Id": "c1", "name": "web"}]
    mock_agent_manager.get_containers.assert_awaited_once_with("Test Agent")


def test_proxy_stacks(auth_client, mock_agent_manager):
    mock_agent_manager.get_stacks.return_value = [{"name": "web"}]
    resp = auth_client.get("/api/stacks", params={"agent": "Test Agent"})
    assert resp.status_code == 200
    assert resp.json() == [{"name": "web"}]


def test_proxy_ports(auth_client, mock_agent_manager):
    mock_agent_manager.get_ports.return_value = [{"port": 8080}]
    resp = auth_client.get("/api/ports", params={"agent": "Test Agent"})
    assert resp.status_code == 200
    assert resp.json() == [{"port": 8080}]


def test_proxy_start_container(auth_client, mock_agent_manager):
    mock_agent_manager.start_container.return_value = True
    resp = auth_client.post(
        "/api/containers/abc/start", params={"agent": "Test Agent"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"success": True}
    mock_agent_manager.start_container.assert_awaited_once_with("Test Agent", "abc")


def test_proxy_stop_container(auth_client, mock_agent_manager):
    mock_agent_manager.stop_container.return_value = True
    resp = auth_client.post("/api/containers/abc/stop", params={"agent": "Test Agent"})
    assert resp.status_code == 200
    assert resp.json() == {"success": True}


def test_proxy_restart_container(auth_client, mock_agent_manager):
    mock_agent_manager.restart_container.return_value = False
    resp = auth_client.post("/api/containers/abc/restart", params={"agent": "Test Agent"})
    assert resp.status_code == 200
    assert resp.json() == {"success": False}


def test_proxy_container_agent_validation(auth_client, mock_agent_manager):
    resp = auth_client.post("/api/containers/abc/start", params={"agent": "ghost"})
    assert resp.status_code == 404
    mock_agent_manager.start_container.assert_not_awaited()


# ---------------------------------------------------------------------------
# SSE endpoints
# ---------------------------------------------------------------------------

def _sse_stream():
    async def _gen():
        yield {"type": "output", "line": "Starting..."}
        yield {"type": "done", "success": True, "output": "Started"}

    return _gen()


def _sse_error_stream():
    async def _gen():
        yield {"type": "error", "error": "oops"}

    return _gen()


def test_sse_stack_start(auth_client, mock_agent_manager):
    # The endpoint calls ``stream_start_stack(...)`` (no await) and iterates the
    # result, so the method must return the generator synchronously.
    from unittest.mock import MagicMock

    mock_agent_manager.stream_start_stack = MagicMock(return_value=_sse_stream())
    resp = auth_client.post("/api/stacks/web/start", params={"agent": "Test Agent"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "event: output" in resp.text
    assert "Starting..." in resp.text
    assert "event: done" in resp.text
    assert '"success": true' in resp.text
    # The on_success callback invalidates the agent cache.
    mock_agent_manager.invalidate_cache.assert_awaited_once_with("Test Agent")


def test_sse_stack_stop(auth_client, mock_agent_manager):
    from unittest.mock import MagicMock

    mock_agent_manager.stream_stop_stack = MagicMock(return_value=_sse_stream())
    resp = auth_client.post("/api/stacks/web/stop", params={"agent": "Test Agent"})
    assert resp.status_code == 200
    assert "event: output" in resp.text


def test_sse_stack_error_event(auth_client, mock_agent_manager):
    from unittest.mock import MagicMock

    mock_agent_manager.stream_restart_stack = MagicMock(return_value=_sse_error_stream())
    resp = auth_client.post("/api/stacks/web/restart", params={"agent": "Test Agent"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "event: error" in resp.text
    assert "oops" in resp.text


def test_sse_update_container_image(auth_client, mock_agent_manager):
    from unittest.mock import MagicMock

    mock_agent_manager.stream_update_container_image = MagicMock(return_value=_sse_stream())
    resp = auth_client.post(
        "/api/containers/abc/update-image", params={"agent": "Test Agent"}
    )
    assert resp.status_code == 200
    assert "event: done" in resp.text
