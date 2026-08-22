"""Smoke tests pour le refactor ``app.routes.api`` → routeurs par domaine.

Vérifie que la façade ``app.routes.api`` continue d'exposer les symboles que
``app.main`` et les tests importent (``router``, ``agent_manager``,
``LLMClient``, ``_check_agent_error``, helpers), que le callback broadcast est
bien injecté sur le singleton ``agent_manager``, que chaque sous-routeur résout
la façade via ``_api()`` sans cycle d'import, et que les 5 routeurs sont inclus
dans le router principal.
"""

import app.routes.api as api
import app.routes.api_helpers as api_helpers
import app.routes.agents as agents
import app.routes.settings as settings
import app.routes.containers as containers
import app.routes.stacks as stacks
import app.routes.chat as chat
from fastapi.routing import APIRouter


def test_facade_exposes_expected_symbols():
    """Le namespace app.routes.api ré-exporte les symboles d'origine."""
    expected = [
        # router principal (utilisé par app.main)
        "router",
        # singleton + LLMClient (monkeypatchés par les tests)
        "agent_manager",
        "LLMClient",
        # helpers (appelés / importés par les tests)
        "_check_agent_error",
        "_resolve_agent",
        "_check_auth",
        "_check_auth_ws",
        "_unauthorized",
        "_sse_response",
        "_sse_action_response",
        "_mask_api_key",
        "_broadcast_agent_event",
        "_events_clients",
        "_save_agents",
        "_VERSION_PATH",
        # symboles app.llm ré-exportés (namespace préservé)
        "run_chat",
        "read_soul",
        "update_soul",
        "execute_tool",
        "build_system_prompt",
        "TOOLS",
        "HUMAN_VALIDATION_MARKER",
    ]
    for name in expected:
        assert hasattr(api, name), f"app.routes.api.{name} manquant"
        assert getattr(api, name) is not None


def test_router_is_an_apirouter_with_api_prefix():
    """Le router principal garde le prefix /api et inclut les 5 routeurs."""
    assert isinstance(api.router, APIRouter)
    assert api.router.prefix == "/api"
    # Les 5 sous-routeurs sont inclus.
    included = [r for r in api.router.routes if type(r).__name__ == "_IncludedRouter"]
    assert len(included) == 5


def test_agent_manager_is_the_same_singleton():
    """agent_manager reste l'objet unique de app.agent_manager.client."""
    from app.agent_manager.client import agent_manager as real

    assert api.agent_manager is real
    # Les sous-routeurs le résolvent via la façade (monkeypatchable).
    for mod in (agents, settings, containers, stacks, chat):
        assert mod._api().agent_manager is real


def test_broadcast_callback_injected():
    """Le callback broadcast est injecté sur le singleton comme avant."""
    from app.agent_manager.client import agent_manager as real

    assert real.broadcast_agent_event is api._broadcast_agent_event
    # _events_clients est le MÊME objet partagé (list module-level).
    assert api._events_clients is api_helpers._events_clients
    assert containers._events_clients is api_helpers._events_clients


def test_api_resolution_no_cycle():
    """Le helper _api() résout la façade sans cycle d'import."""
    assert agents._api() is api
    assert settings._api() is api
    assert containers._api() is api
    assert stacks._api() is api
    assert chat._api() is api
    assert api_helpers._api() is api


def test_shared_helpers_resolve_agent_manager_via_facade():
    """_resolve_agent lit agent_manager via le namespace façade (patchable)."""
    from unittest.mock import MagicMock

    real = api.agent_manager
    try:
        mock = MagicMock()
        mock.agents = {"FAKE": {"status": "online"}}
        api.agent_manager = mock
        name, err = api_helpers._resolve_agent("FAKE")
        assert name == "FAKE"
        assert err is None
        # Un agent inconnu → 404 (comportement identique, résolu au moment de l'appel).
        name2, err2 = api_helpers._resolve_agent("ghost")
        assert name2 is None
        assert err2 is not None and err2.status_code == 404
    finally:
        api.agent_manager = real


def test_llmclient_symbols_shared_with_llm_facade():
    """LLMClient et les symboles llm sont ceux de app.llm.client."""
    import app.llm.client as llm

    assert api.LLMClient is llm.LLMClient
    assert api.TOOLS is llm.TOOLS
    assert api.HUMAN_VALIDATION_MARKER is llm.HUMAN_VALIDATION_MARKER
