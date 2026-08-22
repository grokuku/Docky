"""Smoke tests d'import des sous-modules extraits de docker_manager.

Chaque sous-module ``agent.docker.*`` doit s'importer et être ré-exporté dans
le namespace ``agent.docker_manager`` (façade) afin que routes.py/main.py et
les monkeypatchs existants (ciblant ``agent.docker_manager.<symbole>``)
continuent de fonctionner.
"""

import importlib

import pytest

SUB_MODULES = [
    "agent.docker.validation",
    "agent.docker.update_check",
    "agent.docker.compose_stream",
    "agent.docker.git_history",
    "agent.docker.import_stack",
    "agent.docker.ports",
    "agent.docker.events",
]

# symboles attendus dans le namespace agent.docker_manager, associés au module
# source qui doit les fournir.
RE_EXPORTS = {
    "agent.docker.validation": [
        "validate_stack_name", "validate_filename", "safe_join", "get_stacks_dir", "_stack_dir",
    ],
    "agent.docker.update_check": [
        "check_image_update", "_remote_manifest_check", "_local_repo_digests",
        "_extract_remote_digests", "_dedupe_preserve_order", "_split_image_reference",
        "_canonical_repository", "_invalidate_update_check", "_invalidate_stack_update_cache",
        "_short_digest", "_short_digests", "_remote_distribution_info",
        "_remote_manifest_digests", "_local_repo_digests_for_image",
        "_update_cache_key", "_update_check_cache_info", "_clean_update_check_cache",
        "_UPDATE_CHECK_TTL", "_update_check_cache",
    ],
    "agent.docker.compose_stream": [
        "STREAM_IDLE_TIMEOUT", "STREAM_EVENT_OUTPUT", "STREAM_EVENT_RESULT",
        "StreamCommandError", "_run_compose", "_run_command_stream", "_stream_compose",
        "_stream_compose_step", "_stream_command_step", "_compose_up_command",
        "_compose_down_command", "stream_start_stack", "stream_stop_stack",
        "stream_restart_stack", "stream_update_stack", "stream_deploy_stack",
    ],
    "agent.docker.git_history": [
        "_git_init", "_git_save", "_get_git_history", "_get_git_version",
        "_git_restore", "_git_cleanup", "get_history_settings", "set_history_settings",
    ],
    "agent.docker.import_stack": ["import_stack"],
    "agent.docker.ports": [
        "get_used_ports", "_scan_system_ports", "_parse_ss_output",
        "_parse_netstat_output", "_parse_proc_net",
    ],
    "agent.docker.events": ["watch_docker_events"],
}


@pytest.mark.parametrize("mod_name", SUB_MODULES)
def test_submodule_imports(mod_name):
    importlib.import_module(mod_name)


def test_facade_re_exports_all_symbols():
    from agent import docker_manager as dm

    for mod_name, symbols in RE_EXPORTS.items():
        mod = importlib.import_module(mod_name)
        for sym in symbols:
            assert hasattr(dm, sym), f"agent.docker_manager.{sym} manquant (ré-export de {mod_name})"
            # Les objets "réels" (hors constantes scalaires) doivent venir du sous-module.
            dm_obj = getattr(dm, sym)
            mod_obj = getattr(mod, sym)
            if hasattr(mod_obj, "__module__") or isinstance(mod_obj, type):
                assert dm_obj is mod_obj, f"{sym} n'est pas le même objet que {mod_name}.{sym}"


def test_update_check_cache_is_shared():
    """Le cache _update_check_cache doit être un objet partagé unique."""
    from agent import docker_manager as dm
    from agent.docker import update_check

    assert dm._update_check_cache is update_check._update_check_cache
