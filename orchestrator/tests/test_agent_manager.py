"""Tests for ``app.agent_manager.client.AgentManager``.

Every test instantiates a FRESH ``AgentManager`` after pointing
``DOCKY_DATA_DIR`` at ``tmp_path`` — the global singleton is never relied on.
All HTTP is mocked with respx; time-based cache logic is driven via a fake
clock.
"""

import asyncio
import json

import httpx
import pytest
import yaml
from unittest.mock import AsyncMock

from orchestrator.tests._helpers import make_settings


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_agent_manager(monkeypatch, tmp_path):
    """A brand-new AgentManager bound to ``tmp_path``."""
    monkeypatch.setenv("DOCKY_DATA_DIR", str(tmp_path))
    make_settings(tmp_path)
    from app.agent_manager.client import AgentManager

    return AgentManager()


def _write_settings(tmp_path, agents):
    settings = {
        "server": {"host": "0.0.0.0", "port": 8000},
        "llm": {"endpoint": "", "api_key": "", "model": ""},
        "firecrawl": {"endpoint": "", "api_key": ""},
        "security": {"jwt_secret": "test-jwt-secret", "jwt_algorithm": "HS256", "jwt_expire_minutes": 1440},
        "agents": agents,
    }
    (tmp_path / "settings.yaml").write_text(
        yaml.safe_dump(settings, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# _parse_sse_event
# ---------------------------------------------------------------------------

def test_parse_sse_event_output(fresh_agent_manager):
    evt = fresh_agent_manager._parse_sse_event("output", ['{"line": "hello"}'])
    assert evt == {"type": "output", "line": "hello"}


def test_parse_sse_event_done_success(fresh_agent_manager):
    evt = fresh_agent_manager._parse_sse_event(
        "done", ['{"success": true, "output": "all done"}']
    )
    assert evt == {"type": "done", "success": True, "output": "all done", "error": ""}


def test_parse_sse_event_done_failure_propagates_error(fresh_agent_manager):
    evt = fresh_agent_manager._parse_sse_event(
        "done", ['{"success": false, "output": "partial", "error": "compose failed"}']
    )
    assert evt == {
        "type": "done",
        "success": False,
        "output": "partial",
        "error": "compose failed",
    }


def test_parse_sse_event_error(fresh_agent_manager):
    evt = fresh_agent_manager._parse_sse_event("error", ['{"error": "boom"}'])
    assert evt == {"type": "error", "error": "boom"}


def test_parse_sse_event_error_default_message(fresh_agent_manager):
    evt = fresh_agent_manager._parse_sse_event("error", ["{}"])
    assert evt == {"type": "error", "error": "Erreur inconnue"}


def test_parse_sse_event_empty_returns_none(fresh_agent_manager):
    assert fresh_agent_manager._parse_sse_event(None, []) is None
    assert fresh_agent_manager._parse_sse_event("", []) is None
    assert fresh_agent_manager._parse_sse_event("output", []) is None


def test_parse_sse_event_malformed_json_graceful(fresh_agent_manager):
    # Malformed JSON falls back to {"raw": raw} internally and never crashes;
    # the event is still normalised to a well-formed dict.
    evt = fresh_agent_manager._parse_sse_event("done", ["{not valid json"])
    assert evt == {"type": "done", "success": True, "output": "", "error": ""}
    evt = fresh_agent_manager._parse_sse_event("output", ["not json"])
    assert evt == {"type": "output", "line": ""}


# ---------------------------------------------------------------------------
# translate_path
# ---------------------------------------------------------------------------

def test_translate_path_longest_prefix_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKY_DATA_DIR", str(tmp_path))
    _write_settings(
        tmp_path,
        [
            {
                "name": "Srv",
                "url": "http://agent:8080",
                "api_key": "key",
                "path_mappings": [
                    {"host": "/mnt/data", "local": "/data"},
                    {"host": "/mnt/data/app", "local": "/srv/app"},
                ],
            }
        ],
    )
    from app.agent_manager.client import AgentManager

    manager = AgentManager()
    assert manager.translate_path("Srv", "/mnt/data/app/file") == "/srv/app/file"
    assert manager.translate_path("Srv", "/mnt/data/other") == "/data/other"


def test_translate_path_no_match_returns_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKY_DATA_DIR", str(tmp_path))
    _write_settings(tmp_path, [{"name": "Srv", "url": "http://agent:8080", "api_key": "key"}])
    from app.agent_manager.client import AgentManager

    manager = AgentManager()
    assert manager.translate_path("Srv", "/unmapped/path") == "/unmapped/path"


# ---------------------------------------------------------------------------
# _request
# ---------------------------------------------------------------------------

async def test_request_unknown_agent_raises_value_error(fresh_agent_manager):
    with pytest.raises(ValueError, match="not found"):
        await fresh_agent_manager._request("ghost", "GET", "/agent/containers")


async def test_request_json_returns_dict(respx_mock, fresh_agent_manager):
    route = respx_mock.get("http://agent:8080/agent/containers").mock(
        return_value=httpx.Response(200, json=[{"Id": "abc"}])
    )
    result = await fresh_agent_manager._request("Test Agent", "GET", "/agent/containers")
    assert result == [{"Id": "abc"}]
    assert route.calls.last.request.url == "http://agent:8080/agent/containers"
    assert route.calls.last.request.headers["authorization"] == "Bearer test-key"


async def test_request_text_returns_str(respx_mock, fresh_agent_manager):
    respx_mock.get("http://agent:8080/agent/containers").mock(
        return_value=httpx.Response(200, text="plain answer")
    )
    result = await fresh_agent_manager._request("Test Agent", "GET", "/agent/containers")
    assert result == "plain answer"


async def test_request_http_error_raises_httpstatus(respx_mock, fresh_agent_manager):
    respx_mock.get("http://agent:8080/agent/containers").mock(
        return_value=httpx.Response(500, text="internal error")
    )
    with pytest.raises(httpx.HTTPStatusError):
        await fresh_agent_manager._request("Test Agent", "GET", "/agent/containers")


# ---------------------------------------------------------------------------
# _stream_request / _consume_stream
# ---------------------------------------------------------------------------

async def test_stream_request_parses_multi_events(respx_mock, fresh_agent_manager):
    sse = (
        "event: output\ndata: {\"line\": \"step1\"}\n\n"
        "event: output\ndata: {\"line\": \"step2\"}\n\n"
        "event: done\ndata: {\"success\": true, \"output\": \"ok\"}\n\n"
    )
    respx_mock.post("http://agent:8080/agent/stacks/web/start").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse
        )
    )
    events = [
        e
        async for e in fresh_agent_manager._stream_request("Test Agent", "POST", "/agent/stacks/web/start")
    ]
    assert events == [
        {"type": "output", "line": "step1"},
        {"type": "output", "line": "step2"},
        {"type": "done", "success": True, "output": "ok", "error": ""},
    ]


async def test_stream_request_http_error_raises_runtime(respx_mock, fresh_agent_manager):
    respx_mock.post("http://agent:8080/agent/stacks/web/start").mock(
        return_value=httpx.Response(500, content="boom")
    )
    with pytest.raises(RuntimeError, match="500"):
        async for _ in fresh_agent_manager._stream_request("Test Agent", "POST", "/agent/stacks/web/start"):
            pass


async def test_stream_request_non_sse_content_type_raises_runtime(respx_mock, fresh_agent_manager):
    respx_mock.post("http://agent:8080/agent/stacks/web/start").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "application/json"}, content="{}"
        )
    )
    with pytest.raises(RuntimeError, match="non-streaming"):
        async for _ in fresh_agent_manager._stream_request("Test Agent", "POST", "/agent/stacks/web/start"):
            pass


async def test_consume_stream_aggregates_success(respx_mock, fresh_agent_manager):
    sse = (
        "event: output\ndata: {\"line\": \"l1\"}\n\n"
        "event: output\ndata: {\"line\": \"l2\"}\n\n"
        "event: done\ndata: {\"success\": true, \"output\": \"l1\\nl2\"}\n\n"
    )
    respx_mock.post("http://agent:8080/agent/stacks/web/start").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse
        )
    )
    result = await fresh_agent_manager._consume_stream("Test Agent", "POST", "/agent/stacks/web/start")
    assert result == {"success": True, "output": "l1\nl2"}


async def test_consume_stream_aggregates_failure(respx_mock, fresh_agent_manager):
    sse = (
        "event: output\ndata: {\"line\": \"trying\"}\n\n"
        "event: done\ndata: {\"success\": false, \"error\": \"compose failed\"}\n\n"
    )
    respx_mock.post("http://agent:8080/agent/stacks/web/start").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse
        )
    )
    result = await fresh_agent_manager._consume_stream("Test Agent", "POST", "/agent/stacks/web/start")
    assert result == {"success": False, "output": "trying", "error": "compose failed"}


# ---------------------------------------------------------------------------
# Cache stale-while-revalidate
# ---------------------------------------------------------------------------

def _patch_time(monkeypatch, fresh_agent_manager):
    import app.agent_manager.client as am_mod

    fake = {"now": 1000.0}
    monkeypatch.setattr(am_mod.time, "time", lambda: fake["now"])
    return fake


async def test_cache_fresh_returns_without_refresh(monkeypatch, fresh_agent_manager):
    fake = _patch_time(monkeypatch, fresh_agent_manager)
    manager = fresh_agent_manager
    fetch = AsyncMock(return_value=[{"new": 1}])
    manager._cache["containers"] = {"data": [{"old": 1}], "timestamp": 997.0, "pending": False}

    fake["now"] = 999.0  # 2s old → fresh
    result = manager._get_cached_or_refresh("containers", fetch)
    assert result == [{"old": 1}]
    assert manager._cache["containers"]["pending"] is False
    fetch.assert_not_awaited()


async def test_cache_stale_triggers_background_refresh(monkeypatch, fresh_agent_manager):
    fake = _patch_time(monkeypatch, fresh_agent_manager)
    manager = fresh_agent_manager
    fetch = AsyncMock(return_value=[{"new": 1}])
    manager._cache["containers"] = {"data": [{"old": 1}], "timestamp": 900.0, "pending": False}

    fake["now"] = 1005.0  # 105s old → stale
    result = manager._get_cached_or_refresh("containers", fetch)
    assert result == [{"old": 1}]
    assert manager._cache["containers"]["pending"] is True

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert manager._cache["containers"]["pending"] is False
    assert manager._cache["containers"]["data"] == [{"new": 1}]


async def test_cache_no_cache_returns_none(monkeypatch, fresh_agent_manager):
    _patch_time(monkeypatch, fresh_agent_manager)
    manager = fresh_agent_manager
    fetch = AsyncMock()
    manager._cache["containers"] = {"data": None, "timestamp": 0, "pending": False}
    assert manager._get_cached_or_refresh("containers", fetch) is None


# ---------------------------------------------------------------------------
# ping_agent / ping_all
# ---------------------------------------------------------------------------

async def test_ping_agent_online(respx_mock, fresh_agent_manager):
    respx_mock.get("http://agent:8080/agent/health").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    ok = await fresh_agent_manager.ping_agent("Test Agent")
    assert ok is True
    assert fresh_agent_manager.agents["Test Agent"]["status"] == "online"


async def test_ping_agent_non_200_offline(respx_mock, fresh_agent_manager):
    respx_mock.get("http://agent:8080/agent/health").mock(
        return_value=httpx.Response(500)
    )
    ok = await fresh_agent_manager.ping_agent("Test Agent")
    assert ok is False
    assert fresh_agent_manager.agents["Test Agent"]["status"] == "offline"


async def test_ping_agent_network_error_offline(respx_mock, fresh_agent_manager):
    respx_mock.get("http://agent:8080/agent/health").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    ok = await fresh_agent_manager.ping_agent("Test Agent")
    assert ok is False
    assert fresh_agent_manager.agents["Test Agent"]["status"] == "offline"


async def test_ping_agent_unknown_false(fresh_agent_manager):
    assert await fresh_agent_manager.ping_agent("ghost") is False


async def test_ping_all_updates_status(respx_mock, fresh_agent_manager):
    respx_mock.get("http://agent:8080/agent/health").mock(
        return_value=httpx.Response(200, json={})
    )
    await fresh_agent_manager.ping_all()
    assert fresh_agent_manager.agents["Test Agent"]["status"] == "online"


# ---------------------------------------------------------------------------
# refresh_cache / _rebuild_aggregate_cache / get_all_*
# ---------------------------------------------------------------------------

async def test_refresh_cache_populates_per_agent(monkeypatch, fresh_agent_manager):
    manager = fresh_agent_manager
    manager.get_containers = AsyncMock(return_value=[{"Id": "c1"}])
    manager.get_stacks = AsyncMock(return_value=[{"name": "web"}])
    manager.get_ports = AsyncMock(return_value=[{"port": 8080}])

    await manager.refresh_cache("Test Agent")
    assert manager.cache["Test Agent"]["containers"] == [{"Id": "c1"}]
    assert manager.cache["Test Agent"]["stacks"] == [{"name": "web"}]
    assert manager.cache["Test Agent"]["ports"] == [{"port": 8080}]
    assert "timestamp" in manager.cache["Test Agent"]


async def test_rebuild_aggregate_cache_tags_agent_name(fresh_agent_manager):
    manager = fresh_agent_manager
    import time

    manager.cache = {
        "Test Agent": {
            "containers": [{"name": "c1"}],
            "stacks": [{"name": "web"}],
            "ports": [{"port": 8080}],
            "timestamp": time.time(),
        }
    }
    await manager._rebuild_aggregate_cache()
    assert manager._cache["containers"]["data"] == [{"name": "c1", "agent_name": "Test Agent"}]
    assert manager._cache["stacks"]["data"] == [{"name": "web", "agent_name": "Test Agent"}]
    assert manager._cache["ports"]["data"] == [{"port": 8080, "agent_name": "Test Agent"}]


async def test_get_all_containers_tags_and_ignores_errors(fresh_agent_manager):
    manager = fresh_agent_manager
    manager.agents = {
        "A": {"url": "http://a", "api_key": "k"},
        "B": {"url": "http://b", "api_key": "k"},
        "C": {"url": "http://c", "api_key": "k"},
    }
    manager.get_containers = AsyncMock(
        side_effect=lambda name: [{"name": name}]
        if name != "A"
        else (_ for _ in ()).throw(RuntimeError("down"))
    )
    result = await manager.get_all_containers()
    assert result == [{"name": "B", "agent_name": "B"}, {"name": "C", "agent_name": "C"}]


async def test_get_all_stacks_ignores_non_list(fresh_agent_manager):
    manager = fresh_agent_manager
    manager.agents = {"A": {"url": "http://a", "api_key": "k"}, "B": {"url": "http://b", "api_key": "k"}}
    manager.get_stacks = AsyncMock(
        side_effect=lambda name: "not-a-list" if name == "A" else [{"name": name}]
    )
    result = await manager.get_all_stacks()
    assert result == [{"name": "B", "agent_name": "B"}]


async def test_get_all_ports_exception_ignored(fresh_agent_manager):
    manager = fresh_agent_manager
    manager.agents = {"A": {"url": "http://a", "api_key": "k"}}
    manager.get_ports = AsyncMock(
        side_effect=lambda name: (_ for _ in ()).throw(httpx.ConnectError("no"))
    )
    assert await manager.get_all_ports() == []


async def test_invalidate_cache_clears_and_rebuilds(fresh_agent_manager):
    manager = fresh_agent_manager
    import time

    manager.cache = {
        "Test Agent": {
            "containers": [{"name": "old"}],
            "stacks": [],
            "ports": [],
            "timestamp": time.time(),
        }
    }
    manager._cache["containers"] = {"data": [{"name": "old"}], "timestamp": 0, "pending": False}
    manager.get_containers = AsyncMock(return_value=[{"name": "new"}])
    manager.get_stacks = AsyncMock(return_value=[])
    manager.get_ports = AsyncMock(return_value=[])

    await manager.invalidate_cache()
    assert manager.cache == {}
    assert manager._cache["containers"]["data"] == [{"name": "new", "agent_name": "Test Agent"}]


# ---------------------------------------------------------------------------
# _load_cache / _save_cache
# ---------------------------------------------------------------------------

def test_load_cache_valid_json(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKY_DATA_DIR", str(tmp_path))
    make_settings(tmp_path)
    (tmp_path / "cache.json").write_text(
        json.dumps({"containers": {"data": [{"a": 1}], "timestamp": 10, "pending": False}})
    )
    from app.agent_manager.client import AgentManager

    manager = AgentManager()
    assert manager._cache["containers"]["data"] == [{"a": 1}]
    assert manager._cache["containers"]["timestamp"] == 10
    assert manager._cache["containers"]["pending"] is False


def test_load_cache_corrupt_json_resets(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKY_DATA_DIR", str(tmp_path))
    make_settings(tmp_path)
    (tmp_path / "cache.json").write_text("{ definitely not valid json")
    from app.agent_manager.client import AgentManager

    manager = AgentManager()
    assert manager._cache["containers"]["data"] is None
    assert manager._cache["containers"]["pending"] is False


def test_save_cache_writes_to_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKY_DATA_DIR", str(tmp_path))
    make_settings(tmp_path)
    from app.agent_manager.client import AgentManager

    manager = AgentManager()
    manager._cache["containers"]["data"] = [{"x": 1}]
    manager._save_cache()
    saved = json.loads((tmp_path / "cache.json").read_text())
    assert saved["containers"]["data"] == [{"x": 1}]


# ---------------------------------------------------------------------------
# TLS policy + WebSocket API-key in header
# ---------------------------------------------------------------------------

def _manager_with_agent(tmp_path, monkeypatch, agent_extra=None):
    agent = {"name": "Test Agent", "url": "http://agent:8080", "api_key": "test-key"}
    if agent_extra:
        agent.update(agent_extra)
    _write_settings(tmp_path, [agent])
    monkeypatch.setenv("DOCKY_DATA_DIR", str(tmp_path))
    from app.agent_manager.client import AgentManager
    return AgentManager()


def test_load_agents_tls_verify_defaults_true(fresh_agent_manager):
    info = fresh_agent_manager.agents["Test Agent"]
    assert info["tls_verify"] is True
    assert info["ca_cert"] is None


def test_load_agents_tls_verify_false_logs_warning(tmp_path, monkeypatch, caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        manager = _manager_with_agent(
            tmp_path, monkeypatch, {"tls_verify": False}
        )
    info = manager.agents["Test Agent"]
    assert info["tls_verify"] is False
    assert any(
        "DISABLED" in r.message and "tls_verify=false" in r.message
        for r in caplog.records
    )


def test_load_agents_ca_cert(tmp_path, monkeypatch):
    manager = _manager_with_agent(tmp_path, monkeypatch, {"ca_cert": "/tmp/ca.pem"})
    assert manager.agents["Test Agent"]["ca_cert"] == "/tmp/ca.pem"


def test_agent_tls_options_default_verify_true(fresh_agent_manager):
    opts = fresh_agent_manager._agent_tls_options(fresh_agent_manager.agents["Test Agent"])
    assert opts == {"verify": True}


def test_agent_tls_options_false_when_disabled(tmp_path, monkeypatch):
    manager = _manager_with_agent(tmp_path, monkeypatch, {"tls_verify": False})
    opts = manager._agent_tls_options(manager.agents["Test Agent"])
    assert opts == {"verify": False}


def test_agent_tls_options_uses_ca_cert_path(tmp_path, monkeypatch):
    manager = _manager_with_agent(tmp_path, monkeypatch, {"ca_cert": "/tmp/ca.pem"})
    opts = manager._agent_tls_options(manager.agents["Test Agent"])
    assert opts == {"verify": "/tmp/ca.pem"}


def test_agent_ws_connect_kwargs_bearer_header_no_query(fresh_agent_manager):
    kwargs = fresh_agent_manager._agent_ws_connect_kwargs(
        fresh_agent_manager.agents["Test Agent"]
    )
    assert kwargs["additional_headers"]["Authorization"] == "Bearer test-key"
    # No ssl key when not needed (plain ws default).
    assert "ssl" not in kwargs


def test_agent_ws_connect_kwargs_ssl_context_from_ca_cert(tmp_path, monkeypatch):
    import ssl
    fake_ctx = object()
    monkeypatch.setattr(ssl, "create_default_context", lambda **kw: fake_ctx)
    manager = _manager_with_agent(tmp_path, monkeypatch, {"ca_cert": "/tmp/ca.pem"})
    kwargs = manager._agent_ws_connect_kwargs(manager.agents["Test Agent"])
    assert kwargs["ssl"] is fake_ctx


def test_agent_ws_connect_kwargs_ssl_context_when_verify_disabled(tmp_path, monkeypatch):
    import ssl
    manager = _manager_with_agent(tmp_path, monkeypatch, {"tls_verify": False})
    kwargs = manager._agent_ws_connect_kwargs(manager.agents["Test Agent"])
    assert isinstance(kwargs["ssl"], ssl.SSLContext)
    assert kwargs["ssl"].verify_mode == ssl.CERT_NONE


async def test_connect_agent_events_sends_bearer_header_not_query(
    fresh_agent_manager, monkeypatch
):
    """The orchestrator→agent WS connects with the key in the header."""
    import websockets
    captured = {}

    class FakeWS:
        def __aenter__(self):
            return self

        def __aexit__(self, *a):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(3600)  # block so the loop never iterates

    # websockets.connect is a plain class; the mock must be a plain function
    # returning an async context manager.
    def fake_connect(*args, **kwargs):
        captured["url"] = args[0]
        captured["kwargs"] = kwargs
        return FakeWS()

    monkeypatch.setattr(websockets, "connect", fake_connect)
    task = asyncio.create_task(
        fresh_agent_manager._connect_agent_events("Test Agent")
    )
    await asyncio.sleep(0.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert captured["kwargs"]["additional_headers"]["Authorization"] == "Bearer test-key"
    assert "api_key" not in captured["url"]
    assert captured["url"].endswith("/agent/events")


# ---------------------------------------------------------------------------
# Silent-error logging (falls back to empty value but MUST log a warning)
# ---------------------------------------------------------------------------

async def test_check_update_failure_logs_warning(fresh_agent_manager, respx_mock, caplog):
    """A failed agent call logs a warning while still returning the fallback."""
    import logging
    respx_mock.get("http://agent:8080/agent/containers/c1/update-check").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    with caplog.at_level(logging.WARNING):
        result = await fresh_agent_manager.check_update("Test Agent", "c1")
    # Contract unchanged: fallback dict is returned.
    assert result == {"update_available": False, "error": "Agent unreachable"}
    assert any(
        "check_update failed" in r.message and "c1" in r.message
        for r in caplog.records
    )


async def test_get_container_failure_logs_warning(fresh_agent_manager, respx_mock, caplog):
    """A failed single-container lookup logs a warning while returning None."""
    import logging
    respx_mock.get("http://agent:8080/agent/containers/c1").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    with caplog.at_level(logging.WARNING):
        result = await fresh_agent_manager.get_container("Test Agent", "c1")
    assert result is None
    assert any(
        "get_container failed" in r.message and "c1" in r.message
        for r in caplog.records
    )
