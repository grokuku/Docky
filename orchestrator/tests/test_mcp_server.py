"""Tests for the MCP server (``app.mcp_server``).

Covers server construction, tool registration, tool execution through
``execute_tool`` (with a mocked agent manager), the human-validation marker
for sensitive tools, API-key generation/persistence, and HTTP Bearer auth on
the Streamable HTTP transport.
"""

import json

import pytest
from unittest.mock import AsyncMock

from orchestrator.tests._helpers import make_settings

HUMAN_VALIDATION_MARKER = "__NEEDS_HUMAN_VALIDATION__"


# ---------------------------------------------------------------------------
# Server construction & tool registration
# ---------------------------------------------------------------------------

async def test_build_mcp_server_registers_all_tools():
    from app.mcp_server import build_mcp_server
    from app.llm.tools import TOOLS

    mcp = build_mcp_server()
    assert mcp.name == "Docky"

    expected = {t["function"]["name"] for t in TOOLS}
    assert len(expected) == 30
    tools = await mcp.list_tools()
    assert len(tools) == 30
    assert {t.name for t in tools} == expected


async def test_list_tools_returns_30_tools():
    from app.mcp_server import build_mcp_server

    mcp = build_mcp_server()
    tools = await mcp.list_tools()
    assert len(tools) == 30
    names = {t.name for t in tools}
    for expected in [
        "start_container", "stop_container", "restart_container",
        "start_stack", "stop_stack", "restart_stack",
        "get_container_logs", "exec_in_container", "create_stack",
        "modify_stack_file", "get_stack_files", "read_stack_file",
        "delete_stack", "deploy_stack", "set_file_permissions",
        "get_used_ports", "check_ports_available", "web_search",
        "web_scrape", "web_map", "update_soul", "read_soul",
        "read_compose_reference", "update_stack", "clean_agent",
        "get_container_details", "get_container_stats", "get_stack_status",
        "list_containers", "get_agent_status",
    ]:
        assert expected in names, f"missing tool {expected}"


# ---------------------------------------------------------------------------
# Tool execution through execute_tool (mocked agent manager)
# ---------------------------------------------------------------------------

def _tool_text(result) -> str:
    """Extract the concatenated text from a FastMCP ``ToolResult``."""
    return "".join(part.text for part in result.content)


async def test_call_tool_start_container(mock_agent_manager):
    from app.mcp_server import build_mcp_server

    mock_agent_manager.start_container.return_value = True
    mcp = build_mcp_server()
    result = await mcp.call_tool(
        "start_container", {"agent_name": "Test Agent", "container_id": "abc"}
    )
    assert _tool_text(result) == "Container démarré."
    mock_agent_manager.start_container.assert_awaited_once_with("Test Agent", "abc")


async def test_call_tool_get_agent_status(mock_agent_manager):
    from app.mcp_server import build_mcp_server

    mock_agent_manager.ping_agent.return_value = True
    mcp = build_mcp_server()
    result = await mcp.call_tool("get_agent_status", {"agent_name": "Test Agent"})
    payload = json.loads(_tool_text(result))
    assert payload == {
        "agent_name": "Test Agent",
        "online": True,
        "url": "http://agent:8080",
    }


async def test_call_tool_unknown_tool_raises(mock_agent_manager):
    from app.mcp_server import build_mcp_server

    mcp = build_mcp_server()
    with pytest.raises(Exception):
        await mcp.call_tool("does_not_exist", {})


# ---------------------------------------------------------------------------
# Sensitive tools return the human-validation marker
# ---------------------------------------------------------------------------

async def test_call_tool_exec_in_container_returns_marker(mock_agent_manager):
    from app.mcp_server import build_mcp_server

    mcp = build_mcp_server()
    result = await mcp.call_tool(
        "exec_in_container",
        {"agent_name": "Test Agent", "container_id": "c1", "command": "rm -rf /tmp"},
    )
    text = _tool_text(result)
    assert text.startswith(HUMAN_VALIDATION_MARKER)
    assert "Command: rm -rf /tmp" in text
    mock_agent_manager.exec_container.assert_not_awaited()


async def test_call_tool_clean_agent_returns_marker(mock_agent_manager):
    from app.mcp_server import build_mcp_server

    mcp = build_mcp_server()
    result = await mcp.call_tool("clean_agent", {"agent_name": "Test Agent"})
    text = _tool_text(result)
    assert text.startswith(HUMAN_VALIDATION_MARKER)
    assert "docker system prune -f" in text
    mock_agent_manager.clean_agent.assert_not_awaited()


# ---------------------------------------------------------------------------
# API key generation & persistence
# ---------------------------------------------------------------------------

def test_get_mcp_api_key_generates_and_persists(data_dir):
    from app.mcp_server import get_mcp_api_key

    make_settings(data_dir)  # no mcp_api_key -> must be generated
    key = get_mcp_api_key()
    assert len(key) >= 32

    # Second call returns the same persisted key.
    assert get_mcp_api_key() == key

    # Persisted in settings.yaml under security.mcp_api_key.
    import yaml
    settings = yaml.safe_load((data_dir / "settings.yaml").read_text(encoding="utf-8"))
    assert settings["security"]["mcp_api_key"] == key


def test_get_mcp_api_key_returns_existing(data_dir):
    from app.mcp_server import get_mcp_api_key

    make_settings(data_dir)
    import yaml
    settings = yaml.safe_load((data_dir / "settings.yaml").read_text(encoding="utf-8"))
    settings["security"]["mcp_api_key"] = "pre-set-key"
    (data_dir / "settings.yaml").write_text(
        yaml.safe_dump(settings, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    assert get_mcp_api_key() == "pre-set-key"


# ---------------------------------------------------------------------------
# HTTP Bearer auth on the Streamable HTTP transport
# ---------------------------------------------------------------------------

def _rpc(client, url, payload, headers=None):
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if headers:
        h.update(headers)
    return client.post(url, json=payload, headers=h)


def _init_payload():
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"},
        },
    }


def test_mcp_http_rejects_without_api_key(orchestrator_client):
    from app.mcp_server import get_mcp_api_key

    get_mcp_api_key()  # ensure a key exists
    r = _rpc(orchestrator_client, "/mcp", _init_payload())
    assert r.status_code == 401


def test_mcp_http_rejects_wrong_api_key(orchestrator_client):
    from app.mcp_server import get_mcp_api_key

    get_mcp_api_key()
    r = _rpc(orchestrator_client, "/mcp", _init_payload(), {"Authorization": "Bearer wrong-key"})
    assert r.status_code == 401


def test_mcp_http_accepts_valid_api_key(orchestrator_client):
    from app.mcp_server import get_mcp_api_key

    key = get_mcp_api_key()
    r = _rpc(orchestrator_client, "/mcp", _init_payload(), {"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    assert "serverInfo" in r.text


def test_mcp_http_tools_list_with_valid_key(orchestrator_client):
    from app.mcp_server import get_mcp_api_key

    key = get_mcp_api_key()
    headers = {"Authorization": f"Bearer {key}"}
    assert _rpc(orchestrator_client, "/mcp", _init_payload(), headers).status_code == 200
    assert _rpc(
        orchestrator_client, "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"}, headers
    ).status_code == 202
    r = _rpc(
        orchestrator_client, "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, headers
    )
    assert r.status_code == 200
    assert "start_container" in r.text
