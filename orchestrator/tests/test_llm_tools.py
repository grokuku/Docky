"""Tests for the LLM tool executor (``app.llm.client.execute_tool``) and the
agentic loop (``app.llm.client.run_chat``).

The agent manager is fully mocked (no real network / Docker / LLM); the
Firecrawl helpers and the soul/reference file helpers are the only pieces that
touch real code paths, and they stay inside the test temp dir.
"""

import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from orchestrator.tests._helpers import make_settings

HUMAN_VALIDATION_MARKER = "__NEEDS_HUMAN_VALIDATION__"


# ---------------------------------------------------------------------------
# start / stop / restart container
# ---------------------------------------------------------------------------

async def test_execute_start_container_ok(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.start_container.return_value = True
    result = await execute_tool(
        "start_container", {"agent_name": "Test Agent", "container_id": "abc"}
    )
    assert result == "Container démarré."
    mock_agent_manager.start_container.assert_awaited_once_with("Test Agent", "abc")


async def test_execute_start_container_fail(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.start_container.return_value = False
    result = await execute_tool(
        "start_container", {"agent_name": "Test Agent", "container_id": "abc"}
    )
    assert result == "Échec du démarrage du container."


async def test_execute_stop_container_ok_fail(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.stop_container.return_value = True
    assert await execute_tool("stop_container", {"agent_name": "A", "container_id": "c"}) == "Container arrêté."
    mock_agent_manager.stop_container.return_value = False
    assert await execute_tool("stop_container", {"agent_name": "A", "container_id": "c"}) == "Échec de l'arrêt du container."


async def test_execute_restart_container_ok_fail(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.restart_container.return_value = True
    assert await execute_tool("restart_container", {"agent_name": "A", "container_id": "c"}) == "Container redémarré."
    mock_agent_manager.restart_container.return_value = False
    assert await execute_tool("restart_container", {"agent_name": "A", "container_id": "c"}) == "Échec du redémarrage du container."


# ---------------------------------------------------------------------------
# start / stop / restart stack (_format_stack_result)
# ---------------------------------------------------------------------------

async def test_execute_start_stack_success(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.start_stack.return_value = {"success": True, "output": "line1\nline2"}
    result = await execute_tool("start_stack", {"agent_name": "A", "stack_name": "web"})
    assert result.startswith("Stack démarré.")
    assert "--- output ---" in result
    assert "line1" in result


async def test_execute_start_stack_failure(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.start_stack.return_value = {"success": False, "error": "compose error"}
    result = await execute_tool("start_stack", {"agent_name": "A", "stack_name": "web"})
    assert result == "Échec: compose error"


async def test_execute_stop_stack_success(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.stop_stack.return_value = {"success": True}
    result = await execute_tool("stop_stack", {"agent_name": "A", "stack_name": "web"})
    assert result == "Stack arrêté."


async def test_execute_restart_stack_success(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.restart_stack.return_value = {"success": True, "output": "restarted"}
    result = await execute_tool("restart_stack", {"agent_name": "A", "stack_name": "web"})
    assert result.startswith("Stack redémarré.")


# ---------------------------------------------------------------------------
# get_container_logs
# ---------------------------------------------------------------------------

async def test_execute_get_container_logs_dicts(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.get_container_logs.return_value = [
        {"message": "line1", "stream": "stdout"},
        {"message": "line2", "stream": "stderr"},
    ]
    result = await execute_tool(
        "get_container_logs", {"agent_name": "A", "container_id": "c", "tail": 50}
    )
    assert result == "line1\nline2"
    mock_agent_manager.get_container_logs.assert_awaited_once_with("A", "c", tail=50)


async def test_execute_get_container_logs_empty(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.get_container_logs.return_value = []
    result = await execute_tool("get_container_logs", {"agent_name": "A", "container_id": "c"})
    assert result == "Aucun log disponible."


# ---------------------------------------------------------------------------
# exec_in_container / clean_agent — human validation markers
# ---------------------------------------------------------------------------

async def test_execute_exec_in_container_returns_marker(mock_agent_manager):
    from app.llm.client import execute_tool

    result = await execute_tool(
        "exec_in_container",
        {"agent_name": "Test Agent", "container_id": "c1", "command": "ls -la"},
    )
    assert result.startswith(HUMAN_VALIDATION_MARKER)
    assert "Agent: Test Agent" in result
    assert "Container: c1" in result
    assert "Command: ls -la" in result
    # Must NOT call the agent manager.
    mock_agent_manager.exec_container.assert_not_awaited()


async def test_execute_clean_agent_returns_marker(mock_agent_manager):
    from app.llm.client import execute_tool

    result = await execute_tool("clean_agent", {"agent_name": "Test Agent"})
    assert result.startswith(HUMAN_VALIDATION_MARKER)
    assert "docker system prune -f" in result
    assert "Agent: Test Agent" in result
    mock_agent_manager.clean_agent.assert_not_awaited()


# ---------------------------------------------------------------------------
# create_stack
# ---------------------------------------------------------------------------

async def test_execute_create_stack_success(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.create_stack.return_value = {"success": True, "path": "/stacks/web"}
    result = await execute_tool(
        "create_stack",
        {"agent_name": "Test Agent", "name": "web", "compose_content": "services: {}"},
    )
    assert "créé avec succès" in result
    assert "Chemin: /stacks/web" in result


async def test_execute_create_stack_failure(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.create_stack.return_value = {"success": False, "error": "compose invalid"}
    result = await execute_tool(
        "create_stack",
        {"agent_name": "Test Agent", "name": "web", "compose_content": "bad"},
    )
    assert result.startswith("[error] Échec de la création du stack: compose invalid")


# ---------------------------------------------------------------------------
# modify_stack_file / delete_stack / deploy_stack / set_file_permissions
# ---------------------------------------------------------------------------

async def test_execute_modify_stack_file_success(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.save_stack_file.return_value = {"success": True}
    result = await execute_tool(
        "modify_stack_file",
        {"agent_name": "Test Agent", "stack_name": "web", "filename": "docker-compose.yml", "content": "x"},
    )
    assert "mis à jour" in result
    assert "docker-compose.yml" in result


async def test_execute_modify_stack_file_failure(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.save_stack_file.return_value = {"success": False, "error": "denied"}
    result = await execute_tool(
        "modify_stack_file",
        {"agent_name": "Test Agent", "stack_name": "web", "filename": "docker-compose.yml", "content": "x"},
    )
    assert result.startswith("[error] Échec de la mise à jour")
    assert "denied" in result


async def test_execute_delete_stack_success(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.delete_stack.return_value = {"success": True}
    result = await execute_tool("delete_stack", {"agent_name": "Test Agent", "stack_name": "web"})
    assert result == "Stack 'web' supprimé de l'agent 'Test Agent'."


async def test_execute_delete_stack_failure(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.delete_stack.return_value = {"success": False, "error": "nope"}
    result = await execute_tool("delete_stack", {"agent_name": "Test Agent", "stack_name": "web"})
    assert result.startswith("[error] Échec de la suppression: nope")


async def test_execute_deploy_stack_success(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.deploy_stack.return_value = {"success": True, "output": "done"}
    result = await execute_tool("deploy_stack", {"agent_name": "A", "stack_name": "web"})
    assert "déployé avec succès" in result
    assert "--- output ---" in result


async def test_execute_deploy_stack_failure(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.deploy_stack.return_value = {"success": False, "error": "pull failed"}
    result = await execute_tool("deploy_stack", {"agent_name": "A", "stack_name": "web"})
    assert "échec du déploiement" in result
    assert "--- error ---" in result


async def test_execute_set_file_permissions_success(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.set_permissions.return_value = {"success": True}
    result = await execute_tool(
        "set_file_permissions",
        {"agent_name": "A", "stack_name": "web", "filename": "docker-compose.yml", "mode": "644"},
    )
    assert "Permissions de 'docker-compose.yml'" in result
    assert "définies sur 644" in result


async def test_execute_set_file_permissions_failure(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.set_permissions.return_value = {"success": False, "error": "chmod fail"}
    result = await execute_tool(
        "set_file_permissions",
        {"agent_name": "A", "stack_name": "web", "filename": "docker-compose.yml", "mode": "644"},
    )
    assert result.startswith("[error] Échec chmod: chmod fail")


# ---------------------------------------------------------------------------
# get_used_ports / check_ports_available
# ---------------------------------------------------------------------------

async def test_execute_get_used_ports(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.get_ports.return_value = [
        {"port": 8080, "source": "tcp", "container": "web", "stack": "web"}
    ]
    result = await execute_tool("get_used_ports", {"agent_name": "Test Agent"})
    assert "Ports utilisés (agent 'Test Agent')" in result
    assert "8080 [tcp]" in result
    assert "container: web" in result


async def test_execute_get_used_ports_empty(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.get_ports.return_value = []
    result = await execute_tool("get_used_ports", {"agent_name": "Test Agent"})
    assert result == "Aucun port en écoute détecté sur l'agent 'Test Agent'."


async def test_execute_check_ports_available(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.get_ports.return_value = [{"port": 8080}]
    result = await execute_tool(
        "check_ports_available", {"agent_name": "Test Agent", "ports": [8080, 9090]}
    )
    assert "Port 8080: ❌ déjà utilisé (agent 'Test Agent')" in result
    assert "Port 9090: ✅ disponible (agent 'Test Agent')" in result


# ---------------------------------------------------------------------------
# JSON-serialising tools
# ---------------------------------------------------------------------------

async def test_execute_get_stack_files(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.get_stack_files.return_value = [{"filename": "docker-compose.yml"}]
    result = await execute_tool("get_stack_files", {"agent_name": "A", "stack_name": "web"})
    assert json.loads(result) == {"files": [{"filename": "docker-compose.yml"}]}
    mock_agent_manager.get_stack_files.assert_awaited_once_with("A", "web", include_hidden=True)


async def test_execute_read_stack_file(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.get_stack_file.return_value = "version: '3'"
    result = await execute_tool(
        "read_stack_file", {"agent_name": "A", "stack_name": "web", "filename": "docker-compose.yml"}
    )
    assert json.loads(result) == {"filename": "docker-compose.yml", "content": "version: '3'"}


async def test_execute_update_stack(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.update_stack.return_value = {"success": True, "output": "updated"}
    result = await execute_tool("update_stack", {"agent_name": "A", "stack_name": "web"})
    assert json.loads(result) == {"success": True, "output": "updated"}


async def test_execute_get_container_details(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.get_container.return_value = {"id": "abc", "name": "web"}
    result = await execute_tool("get_container_details", {"agent_name": "A", "container_id": "abc"})
    assert json.loads(result) == {"id": "abc", "name": "web"}


async def test_execute_get_container_stats(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.get_container_stats.return_value = {"cpu": 1.5, "mem": 1024}
    result = await execute_tool("get_container_stats", {"agent_name": "A", "container_id": "abc"})
    assert json.loads(result) == {"cpu": 1.5, "mem": 1024}


async def test_execute_get_stack_status(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.get_stacks.return_value = [{"name": "web", "has_compose": True}]
    mock_agent_manager.get_containers.return_value = [{"name": "c1", "stack": "web"}]
    result = await execute_tool("get_stack_status", {"agent_name": "A", "stack_name": "web"})
    payload = json.loads(result)
    assert payload["stack"] == [{"name": "web", "has_compose": True}]
    assert payload["containers"] == [{"name": "c1", "stack": "web"}]


async def test_execute_list_containers(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.get_containers.return_value = [{"name": "c1"}]
    result = await execute_tool("list_containers", {"agent_name": "Test Agent"})
    assert json.loads(result) == [{"name": "c1"}]


async def test_execute_list_containers_all(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.get_all_containers.return_value = [{"name": "c1", "agent_name": "A"}]
    result = await execute_tool("list_containers", {"agent_name": "all"})
    assert json.loads(result) == [{"name": "c1", "agent_name": "A"}]


async def test_execute_get_agent_status(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.ping_agent.return_value = True
    result = await execute_tool("get_agent_status", {"agent_name": "Test Agent"})
    assert json.loads(result) == {
        "agent_name": "Test Agent",
        "online": True,
        "url": "http://agent:8080",
    }


# ---------------------------------------------------------------------------
# Web tools (Firecrawl helpers)
# ---------------------------------------------------------------------------

async def test_execute_web_search(monkeypatch, mock_agent_manager):
    import app.llm.client as llm_mod
    from app.llm.client import execute_tool

    fake = AsyncMock(return_value="résultats de recherche")
    monkeypatch.setattr(llm_mod, "firecrawl_search", fake)
    result = await execute_tool("web_search", {"query": "nginx", "limit": 3})
    assert result == "résultats de recherche"
    fake.assert_awaited_once_with("nginx", limit=3)


async def test_execute_web_scrape(monkeypatch, mock_agent_manager):
    import app.llm.client as llm_mod
    from app.llm.client import execute_tool

    fake = AsyncMock(return_value="contenu de la page")
    monkeypatch.setattr(llm_mod, "firecrawl_scrape", fake)
    result = await execute_tool("web_scrape", {"url": "https://example.com"})
    assert result == "contenu de la page"
    fake.assert_awaited_once_with("https://example.com")


async def test_execute_web_map(monkeypatch, mock_agent_manager):
    import app.llm.client as llm_mod
    from app.llm.client import execute_tool

    fake = AsyncMock(return_value="sitemap")
    monkeypatch.setattr(llm_mod, "firecrawl_map", fake)
    result = await execute_tool("web_map", {"url": "https://example.com"})
    assert result == "sitemap"
    fake.assert_awaited_once_with("https://example.com")


# ---------------------------------------------------------------------------
# Soul / compose reference
# ---------------------------------------------------------------------------

async def test_execute_update_soul_and_read_soul(mock_agent_manager, data_dir):
    from app.llm.client import execute_tool

    result = await execute_tool("update_soul", {"content": "nouvelle mémoire"})
    assert result == "soul.md updated successfully."
    assert (data_dir / "soul.md").read_text(encoding="utf-8") == "nouvelle mémoire"

    result = await execute_tool("read_soul", {})
    assert result == "nouvelle mémoire"


async def test_execute_read_soul_empty(mock_agent_manager, data_dir):
    from app.llm.client import execute_tool

    if (data_dir / "soul.md").exists():
        (data_dir / "soul.md").unlink()
    result = await execute_tool("read_soul", {})
    assert result == "soul.md est vide."


async def test_execute_read_compose_reference(mock_agent_manager, data_dir):
    from app.llm.client import execute_tool

    (data_dir / "compose_reference.md").write_text("# Reference\n- Ne pas utiliser version:", encoding="utf-8")
    result = await execute_tool("read_compose_reference", {})
    assert "# Reference" in result
    assert "Ne pas utiliser version:" in result


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

async def test_execute_unknown_tool(mock_agent_manager):
    from app.llm.client import execute_tool

    assert await execute_tool("does_not_exist", {}) == "[error] Outil inconnu: does_not_exist"


async def test_execute_missing_argument_key_error(mock_agent_manager):
    from app.llm.client import execute_tool

    result = await execute_tool("start_container", {})
    assert result.startswith("[error] Argument manquant:")
    assert "agent_name" in result


async def test_execute_exception_formatted(mock_agent_manager):
    from app.llm.client import execute_tool

    mock_agent_manager.start_container.side_effect = ValueError("bad container id")
    result = await execute_tool("start_container", {"agent_name": "A", "container_id": "c"})
    # ValueError is caught by its dedicated handler (message only, no type name).
    assert result == "[error] bad container id"

    mock_agent_manager.start_container.side_effect = RuntimeError("crash")
    result = await execute_tool("start_container", {"agent_name": "A", "container_id": "c"})
    assert result == "[error] RuntimeError: crash"


# ---------------------------------------------------------------------------
# TOOLS catalogue sanity
# ---------------------------------------------------------------------------

def test_tools_catalogue_has_30_tools():
    from app.llm.client import TOOLS

    names = [t["function"]["name"] for t in TOOLS]
    assert len(names) == 30
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
# run_chat
# ---------------------------------------------------------------------------

def _resp_final(content):
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def _resp_tool_call(tool_name, arguments_json, call_id="call_1"):
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": tool_name, "arguments": arguments_json},
                        }
                    ],
                }
            }
        ]
    }


def _patch_run_chat_deps(monkeypatch, chat_side_effect, execute_side_effect=None):
    """Monkeypatch LLMClient / build_system_prompt / execute_tool in the module."""
    import app.llm.client as llm_mod

    fake = MagicMock()
    fake.is_configured.return_value = True
    fake.chat = AsyncMock(side_effect=chat_side_effect)
    monkeypatch.setattr(llm_mod, "LLMClient", lambda: fake)
    monkeypatch.setattr(llm_mod, "build_system_prompt", AsyncMock(return_value="system prompt"))
    if execute_side_effect is not None:
        monkeypatch.setattr(llm_mod, "execute_tool", AsyncMock(side_effect=execute_side_effect))
    return fake


async def test_run_chat_not_configured(monkeypatch):
    import app.llm.client as llm_mod

    fake = MagicMock()
    fake.is_configured.return_value = False
    monkeypatch.setattr(llm_mod, "LLMClient", lambda: fake)

    result = await llm_mod.run_chat("bonjour", [{"role": "user", "content": "avant"}])
    assert "n'est pas configuré" in result["response"]
    assert result["tool_calls_made"] == []
    assert result["needs_human_validation"] == []


async def test_run_chat_final_response_no_tool_calls(monkeypatch):
    import app.llm.client as llm_mod

    _patch_run_chat_deps(monkeypatch, chat_side_effect=lambda *a, **k: _resp_final("Réponse finale"))
    result = await llm_mod.run_chat("salut", [{"role": "user", "content": "historique"}])

    assert result["response"] == "Réponse finale"
    assert result["tool_calls_made"] == []
    roles = [m["role"] for m in result["history"]]
    assert "system" not in roles
    assert "user" in roles


async def test_run_chat_tool_call_then_response(monkeypatch):
    import app.llm.client as llm_mod

    args_json = '{"agent_name": "Test Agent", "container_id": "abc"}'
    responses = iter(
        [_resp_tool_call("start_container", args_json), _resp_final("Container démarré !")]
    )
    _patch_run_chat_deps(
        monkeypatch,
        chat_side_effect=lambda *a, **k: next(responses),
        execute_side_effect=lambda *a, **k: "Container démarré.",
    )
    result = await llm_mod.run_chat("démarre le container", [])

    assert result["tool_calls_made"] == [
        {"name": "start_container", "arguments": {"agent_name": "Test Agent", "container_id": "abc"}, "id": "call_1"}
    ]
    assert result["response"] == "Container démarré !"
    roles = [m["role"] for m in result["history"]]
    assert "system" not in roles
    assert "tool" in roles


async def test_run_chat_exec_in_container_needs_validation(monkeypatch):
    import app.llm.client as llm_mod

    args_json = '{"agent_name": "Test Agent", "container_id": "c1", "command": "rm -rf /tmp"}'
    marker = (
        f"{HUMAN_VALIDATION_MARKER}\nAgent: Test Agent\nContainer: c1\nCommand: rm -rf /tmp"
    )
    responses = iter(
        [_resp_tool_call("exec_in_container", args_json, call_id="call_x"), _resp_final("En attente")]
    )
    _patch_run_chat_deps(
        monkeypatch,
        chat_side_effect=lambda *a, **k: next(responses),
        execute_side_effect=lambda *a, **k: marker,
    )
    result = await llm_mod.run_chat("exécute la commande", [])

    assert result["needs_human_validation"] == [
        {
            "name": "exec_in_container",
            "arguments": {"agent_name": "Test Agent", "container_id": "c1", "command": "rm -rf /tmp"},
            "id": "call_x",
        }
    ]
    # A placeholder was sent back to the LLM instead of the raw marker.
    tool_msgs = [m for m in result["history"] if m["role"] == "tool"]
    assert tool_msgs
    assert "validation humaine" in tool_msgs[-1]["content"]
    assert HUMAN_VALIDATION_MARKER not in tool_msgs[-1]["content"]


async def test_run_chat_llm_runtime_error(monkeypatch):
    import app.llm.client as llm_mod

    _patch_run_chat_deps(
        monkeypatch,
        chat_side_effect=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("API down")),
    )
    result = await llm_mod.run_chat("bonjour", [])
    assert result["response"].startswith("Erreur LLM:")
    assert "API down" in result["response"]
    assert result["tool_calls_made"] == []


async def test_run_chat_round_limit_reached(monkeypatch):
    import app.llm.client as llm_mod

    responses = _resp_tool_call("read_soul", "{}", call_id="call_repeat")
    _patch_run_chat_deps(
        monkeypatch,
        chat_side_effect=lambda *a, **k: responses,
        execute_side_effect=lambda *a, **k: "contenu",
    )
    result = await llm_mod.run_chat("boucle", [])
    assert "limite" in result["response"]
    assert len(result["tool_calls_made"]) == 20
