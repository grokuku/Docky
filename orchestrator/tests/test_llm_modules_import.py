"""Smoke tests pour le refactor ``app.llm.client`` → sous-modules.

Vérifie que la façade ``app.llm.client`` continue d'exposer exactement les
mêmes symboles qu'avant le découpage (routes + tests les importent à
l'identique), que le singleton ``agent_manager`` reste le MÊME objet, et que
les sous-modules se résolvent les uns les autres sans cycle (helper ``_client()``).
"""

import app.llm.client as llm
import app.llm.constants as constants
import app.llm.prompt as prompt
import app.llm.soul as soul
import app.llm.tools as tools
import app.llm.web as web


def test_client_facade_exposes_expected_symbols():
    """Le namespace app.llm.client ré-exporte tous les symboles d'origine."""
    expected = [
        # LLM + loop (définis dans la façade)
        "LLMClient",
        "run_chat",
        # prompt
        "build_system_prompt",
        "parse_compose_metadata",
        "_format_container_ports",
        # soul
        "read_soul",
        "update_soul",
        "_soul_path",
        # tools
        "TOOLS",
        "execute_tool",
        "_format_stack_result",
        # web
        "firecrawl_search",
        "firecrawl_scrape",
        "firecrawl_map",
        "_get_web_endpoint",
        "_firecrawl_headers",
        # constants
        "HUMAN_VALIDATION_MARKER",
        "MAX_TOOL_ROUNDS",
        "_DEFAULT_WEB_ENDPOINT",
        "_TOOLS_DOCKER_AGENT_PARAM",
    ]
    for name in expected:
        assert hasattr(llm, name), f"app.llm.client.{name} manquant"
        assert getattr(llm, name) is not None


def test_agent_manager_is_the_same_singleton():
    """agent_manager reste l'objet unique de app.agent_manager.client."""
    from app.agent_manager.client import agent_manager as real

    assert llm.agent_manager is real
    # Les sous-modules le résolvent via la façade (monkeypatchable).
    assert prompt._client().agent_manager is real
    assert tools._client().agent_manager is real


def test_client_resolution_no_cycle():
    """Le helper _client() résout la façade sans cycle."""
    assert prompt._client() is llm
    assert tools._client() is llm


def test_constants_are_shared_objects():
    """Les constantes sont les MÊMES objets partagés (pas de copie)."""
    assert llm.HUMAN_VALIDATION_MARKER is constants.HUMAN_VALIDATION_MARKER
    assert llm.MAX_TOOL_ROUNDS is constants.MAX_TOOL_ROUNDS
    assert llm._DEFAULT_WEB_ENDPOINT is constants._DEFAULT_WEB_ENDPOINT
    assert llm._TOOLS_DOCKER_AGENT_PARAM is constants._TOOLS_DOCKER_AGENT_PARAM
    assert tools.HUMAN_VALIDATION_MARKER is constants.HUMAN_VALIDATION_MARKER
    assert tools._TOOLS_DOCKER_AGENT_PARAM is constants._TOOLS_DOCKER_AGENT_PARAM


def test_tools_and_soul_shared_references():
    """TOOLS / execute_tool / read_soul sont les mêmes objets que dans les sous-modules."""
    assert llm.TOOLS is tools.TOOLS
    assert llm.execute_tool is tools.execute_tool
    assert llm.build_system_prompt is prompt.build_system_prompt
    assert llm.read_soul is soul.read_soul
    assert llm.update_soul is soul.update_soul
    assert llm.firecrawl_search is web.firecrawl_search
    assert len(llm.TOOLS) == 30
