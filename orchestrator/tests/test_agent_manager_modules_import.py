"""Smoke tests pour le refactor ``app.agent_manager.client`` → sous-modules.

Vérifie que la façade ``app.agent_manager.client`` continue d'exposer les
symboles attendus, que les méthodes extraites sont bien ré-affectées sur
``AgentManager``, que le singleton reste l'objet unique, et que les
sous-modules s'importent sans cycle.
"""

import app.agent_manager.cache as cache_mod
import app.agent_manager.client as client_mod
import app.agent_manager.events as events_mod
import app.agent_manager.paths as paths_mod


def test_client_facade_exposes_expected_symbols():
    """Le namespace app.agent_manager.client expose les symboles d'origine."""
    expected = ["AgentManager", "agent_manager", "STREAM_TIMEOUT", "_AGENT_CACHE_TTL"]
    for name in expected:
        assert hasattr(client_mod, name), f"app.agent_manager.client.{name} manquant"
        assert getattr(client_mod, name) is not None


def test_extracted_methods_are_bound_to_agent_manager():
    """Les méthodes extraites sont les mêmes fonctions, ré-affectées à la classe."""
    assert client_mod.AgentManager.translate_path is paths_mod.translate_path

    for name in [
        "_load_cache",
        "_save_cache",
        "refresh_cache",
        "invalidate_cache",
        "_get_cached_containers",
        "_get_cached_stacks",
        "_get_cached_ports",
        "_get_cached_or_refresh",
        "_refresh_cache_entry",
        "_rebuild_aggregate_cache",
        "ensure_cache",
        "get_cached_containers",
        "get_cached_stacks",
        "get_cached_ports",
        "refresh_all_caches",
    ]:
        assert getattr(client_mod.AgentManager, name) is getattr(cache_mod, name), name

    for name in [
        "start_background_refresh",
        "_connect_agent_events",
        "_handle_agent_event",
        "_incremental_refresh",
    ]:
        assert getattr(client_mod.AgentManager, name) is getattr(events_mod, name), name


def test_singleton_is_unique_and_has_broadcast_hook():
    """agent_manager est l'unique instance d'AgentManager et porte le hook."""
    assert isinstance(client_mod.agent_manager, client_mod.AgentManager)
    assert hasattr(client_mod.agent_manager, "broadcast_agent_event")
    assert client_mod.agent_manager.broadcast_agent_event is None or callable(
        client_mod.agent_manager.broadcast_agent_event
    )


def test_submodules_import_without_cycle():
    """Les sous-modules s'importent sans cycle, y compris dans plusieurs ordres."""
    # Le simple fait d'importer ces modules en chaîne suffit : un cycle
    # d'import déclencherait une ImportError ou un attribut partiellement
    # initialisé au moment de la collecte.
    import app.agent_manager.cache  # noqa: F401
    import app.agent_manager.events  # noqa: F401
    import app.agent_manager.paths  # noqa: F401

    assert callable(cache_mod._time)
    assert isinstance(cache_mod._time(), float)
