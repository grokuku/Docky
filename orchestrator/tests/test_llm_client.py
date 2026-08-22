"""Tests for ``app.llm.client``.

Covers the pure parsing helpers, the ``LLMClient`` HTTP layer (via respx),
and the async ``build_system_prompt`` (with a mocked agent manager — no real
network / Docker / LLM).
"""

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock

from orchestrator.tests._helpers import make_settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_settings(data_dir, llm=None, firecrawl=None, agents=None):
    """Write settings.yaml with a custom llm/firecrawl/agents section."""
    settings = {
        "server": {"host": "0.0.0.0", "port": 8000},
        "llm": llm if llm is not None else {"endpoint": "", "api_key": "", "model": ""},
        "firecrawl": firecrawl
        if firecrawl is not None
        else {"endpoint": "", "api_key": ""},
        "security": {"jwt_secret": "test-jwt-secret", "jwt_algorithm": "HS256", "jwt_expire_minutes": 1440},
        "agents": agents
        if agents is not None
        else [
            {
                "name": "Test Agent",
                "url": "http://agent:8080",
                "api_key": "test-key",
            }
        ],
    }
    path = data_dir / "settings.yaml"
    path.write_text(
        __import__("yaml").safe_dump(settings, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    return path


def _llm_settings(data_dir):
    return _write_settings(
        data_dir,
        llm={"endpoint": "http://llm.test/v1", "api_key": "sk-test", "model": "test-model"},
    )


# ---------------------------------------------------------------------------
# parse_compose_metadata
# ---------------------------------------------------------------------------

def test_parse_compose_metadata_extracts_keys(data_dir):
    from app.llm.client import parse_compose_metadata

    content = (
        "# @name: Nginx\n"
        "# @category: Web\n"
        "# @description: Serveur web principal\n"
        "# @ports: 8080:80\n"
        "# @hardware: 1GB\n"
        "services:\n"
        "  web:\n"
        "    image: nginx\n"
    )
    meta = parse_compose_metadata(content)
    assert meta["name"] == "Nginx"
    assert meta["category"] == "Web"
    assert meta["description"] == "Serveur web principal"
    assert meta["ports"] == "8080:80"
    assert meta["hardware"] == "1GB"


def test_parse_compose_metadata_no_metadata_returns_empty(data_dir):
    from app.llm.client import parse_compose_metadata

    assert parse_compose_metadata("services:\n  web:\n    image: nginx\n") == {}
    assert parse_compose_metadata("") == {}


def test_parse_compose_metadata_handles_spaces(data_dir):
    from app.llm.client import parse_compose_metadata

    content = "#   @name:     Espace    \n# @category:   Base de données\n"
    meta = parse_compose_metadata(content)
    assert meta["name"] == "Espace"
    assert meta["category"] == "Base de données"


# ---------------------------------------------------------------------------
# _format_container_ports
# ---------------------------------------------------------------------------

def test_format_container_ports_empty(data_dir):
    from app.llm.client import _format_container_ports

    assert _format_container_ports({"ports": []}) == "aucun"
    assert _format_container_ports({}) == "aucun"


def test_format_container_ports_mixed(data_dir):
    from app.llm.client import _format_container_ports

    container = {
        "ports": [
            {"host_port": "8080", "public_port": "9999", "container": "80"},
            {"public_port": "9090", "container": "81"},
            {"container": "82"},
            {"host_port": None, "public_port": None},
            None,
        ]
    }
    assert _format_container_ports(container) == "8080, 9090, 82"


def test_format_container_ports_none_filtered(data_dir):
    from app.llm.client import _format_container_ports

    container = {
        "ports": [
            {"host_port": None, "public_port": None, "container": None},
            {"host_port": "3000"},
        ]
    }
    assert _format_container_ports(container) == "3000"


# ---------------------------------------------------------------------------
# LLMClient — is_configured
# ---------------------------------------------------------------------------

def test_is_configured_true(data_dir):
    _llm_settings(data_dir)
    from app.llm.client import LLMClient

    assert LLMClient().is_configured() is True


def test_is_configured_false(data_dir):
    make_settings(data_dir)
    from app.llm.client import LLMClient

    assert LLMClient().is_configured() is False


# ---------------------------------------------------------------------------
# LLMClient — chat
# ---------------------------------------------------------------------------

async def test_chat_not_configured_raises(data_dir):
    make_settings(data_dir)
    from app.llm.client import LLMClient

    llm = LLMClient()
    with pytest.raises(RuntimeError, match="not configured"):
        await llm.chat([{"role": "user", "content": "hi"}])


async def test_chat_http_4xx_raises_runtime_error(respx_mock, data_dir):
    _llm_settings(data_dir)
    respx_mock.post("http://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(429, text="rate limited")
    )
    from app.llm.client import LLMClient

    with pytest.raises(RuntimeError, match="429"):
        await LLMClient().chat([{"role": "user", "content": "hi"}])


async def test_chat_http_5xx_raises_runtime_error(respx_mock, data_dir):
    _llm_settings(data_dir)
    respx_mock.post("http://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(500, text="boom")
    )
    from app.llm.client import LLMClient

    with pytest.raises(RuntimeError, match="500"):
        await LLMClient().chat([{"role": "user", "content": "hi"}])


async def test_chat_network_error_raises_runtime_error(respx_mock, data_dir):
    _llm_settings(data_dir)
    respx_mock.post("http://llm.test/v1/chat/completions").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    from app.llm.client import LLMClient

    with pytest.raises(RuntimeError, match="request error"):
        await LLMClient().chat([{"role": "user", "content": "hi"}])


async def test_chat_success_returns_json(respx_mock, data_dir):
    _llm_settings(data_dir)
    payload = {
        "id": "chatcmpl-1",
        "choices": [{"message": {"role": "assistant", "content": "Bonjour !"}}],
    }
    respx_mock.post("http://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=payload)
    )
    from app.llm.client import LLMClient

    result = await LLMClient().chat([{"role": "user", "content": "hi"}])
    assert result == payload
    assert result["choices"][0]["message"]["content"] == "Bonjour !"


# ---------------------------------------------------------------------------
# LLMClient — chat_stream
# ---------------------------------------------------------------------------

async def test_chat_stream_not_configured_raises(data_dir):
    make_settings(data_dir)
    from app.llm.client import LLMClient

    llm = LLMClient()
    with pytest.raises(RuntimeError, match="not configured"):
        async for _ in llm.chat_stream([{"role": "user", "content": "hi"}]):
            pass


async def test_chat_stream_yields_deltas(respx_mock, data_dir):
    _llm_settings(data_dir)
    sse_body = (
        'data: {"choices":[{"delta":{"content":"Bon"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"jour"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    respx_mock.post("http://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=sse_body)
    )
    from app.llm.client import LLMClient

    chunks = [c async for c in LLMClient().chat_stream([{"role": "user", "content": "hi"}])]
    contents = [
        c["choices"][0]["delta"].get("content")
        for c in chunks
        if c.get("choices") and c["choices"][0].get("delta", {}).get("content")
    ]
    assert contents == ["Bon", "jour"]


async def test_chat_stream_yields_tool_calls(respx_mock, data_dir):
    _llm_settings(data_dir)
    sse_body = (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1",'
        '"function":{"name":"start_container","arguments":"{\\"agent_name\\":'
        '\\"Test Agent\\"}"}}]}}]}\n\n'
        "data: [DONE]\n\n"
    )
    respx_mock.post("http://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=sse_body)
    )
    from app.llm.client import LLMClient

    chunks = [c async for c in LLMClient().chat_stream([{"role": "user", "content": "go"}])]
    tool_call_chunks = [c for c in chunks if c.get("choices") and c["choices"][0].get("delta", {}).get("tool_calls")]
    assert tool_call_chunks
    delta_tc = tool_call_chunks[0]["choices"][0]["delta"]["tool_calls"][0]
    assert delta_tc["id"] == "call_1"
    assert delta_tc["function"]["name"] == "start_container"


# ---------------------------------------------------------------------------
# build_system_prompt
# ---------------------------------------------------------------------------

def _prompt_manager(agents, containers, stacks, ports, stack_file="", soul="", ping_fails=False):
    """Build a mocked agent manager for ``build_system_prompt``."""
    mm = MagicMock()
    mm.list_agents.return_value = agents
    if ping_fails:
        mm.ping_all = AsyncMock(side_effect=RuntimeError("no network"))
    else:
        mm.ping_all = AsyncMock(return_value=None)
    mm.get_all_containers = AsyncMock(return_value=containers)
    mm.get_all_stacks = AsyncMock(return_value=stacks)
    mm.get_all_ports = AsyncMock(return_value=ports)
    mm.get_stack_file = AsyncMock(return_value=stack_file)
    return mm


async def test_build_system_prompt_no_agents(monkeypatch, data_dir):
    import app.llm.client as llm_mod

    mm = _prompt_manager([], [], [], [])
    monkeypatch.setattr(llm_mod, "agent_manager", mm)
    prompt = await llm_mod.build_system_prompt()
    assert "Aucun agent configuré." in prompt
    assert "## Agents disponibles" in prompt


async def test_build_system_prompt_sections_present(monkeypatch, data_dir):
    import app.llm.client as llm_mod

    (data_dir / "soul.md").write_text("mémoire persistante du docky", encoding="utf-8")

    agents = [{"name": "Test Agent", "url": "http://agent:8080", "status": "online"}]
    containers = [
        {
            "agent_name": "Test Agent",
            "name": "web",
            "status": "running",
            "image": "nginx",
            "stack": "web",
            "ports": [{"host_port": "8080"}],
        }
    ]
    stacks = [{"agent_name": "Test Agent", "name": "web", "has_compose": True, "has_env": False}]
    ports = [{"agent_name": "Test Agent", "port": 8080, "container": "web"}]
    compose = (
        "# @name: web\n# @category: Web\n# @description: Serveur web\n# @ports: 8080:80\n"
        "services:\n  web:\n    image: nginx\n"
    )

    mm = _prompt_manager(agents, containers, stacks, ports, stack_file=compose)
    monkeypatch.setattr(llm_mod, "agent_manager", mm)

    prompt = await llm_mod.build_system_prompt()
    assert "## Agents disponibles" in prompt
    assert "Test Agent (http://agent:8080) [ONLINE]" in prompt
    assert "## Containers (Test Agent)" in prompt
    assert "web (running)" in prompt
    assert "## Stacks (Test Agent)" in prompt
    assert "## Ports utilisés (Test Agent)" in prompt
    assert "## Mémoire persistante (soul.md)" in prompt
    assert "mémoire persistante du docky" in prompt


async def test_build_system_prompt_get_all_raise_still_builds(monkeypatch, data_dir):
    import app.llm.client as llm_mod

    agents = [{"name": "Test Agent", "url": "http://agent:8080", "status": "offline"}]
    mm = _prompt_manager(agents, [], [], [])
    mm.get_all_containers = AsyncMock(side_effect=RuntimeError("containers down"))
    mm.get_all_stacks = AsyncMock(side_effect=RuntimeError("stacks down"))
    mm.get_all_ports = AsyncMock(side_effect=RuntimeError("ports down"))
    monkeypatch.setattr(llm_mod, "agent_manager", mm)

    prompt = await llm_mod.build_system_prompt()
    assert "## Agents disponibles" in prompt
    assert "## Containers (Test Agent)" in prompt  # fallback "Aucun container détecté."
    assert "Aucun container détecté." in prompt
    assert "## Mémoire persistante (soul.md)" in prompt
