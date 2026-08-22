"""Shared fixtures for the orchestrator test-suite (PHASE 2).

Everything is fully hermetic: no real network / LLM / Docker daemon, and no
access to ``/projects/Docky/data`` or ``/data``. The root ``conftest.py``
already fixed ``DOCKY_DATA_DIR`` to a session temp dir *before* any
``app.*`` import, and provides ``data_dir``, ``make_settings``,
``make_users`` and ``make_api_keys``.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

# Re-export the root-conftest helpers (stable import, see ``_helpers``).
from orchestrator.tests._helpers import (
    BCRYPT_DOCKY123,
    make_api_keys,
    make_settings,
    make_users,
)


# ---------------------------------------------------------------------------
# CSRF bypass (see docs/csrf-protection.md §6)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def disable_csrf_for_tests(monkeypatch):
    """Disable the CSRF double-submit check for every orchestrator test.

    The existing suite performs mutating requests (POST/PUT/DELETE on /api/*,
    POST /login, POST /change-password) without any CSRF material and must
    keep passing UNMODIFIED. ``app.auth.csrf.csrf_enabled()`` short-circuits
    to False whenever ``DOCKY_DISABLE_CSRF_FOR_TESTS`` is set in the process
    environment — a test-only escape hatch (same philosophy as
    ``DOCKY_DATA_DIR``), not a production switch.

    The dedicated CSRF tests (``test_csrf.py``) opt back IN via their own
    ``csrf_on`` fixture, which deletes this variable (autouse fixtures run
    before test-local ones at the same scope, so deletion wins).
    """
    monkeypatch.setenv("DOCKY_DISABLE_CSRF_FOR_TESTS", "1")
    yield


# ---------------------------------------------------------------------------
# Config seeding
# ---------------------------------------------------------------------------

@pytest.fixture
def settings_file(data_dir):
    """Write a deterministic ``settings.yaml`` (fixed JWT secret) and return it."""
    return make_settings(data_dir)


@pytest.fixture
def users_file(data_dir):
    """Write a deterministic ``users.yaml`` (admin / docky123) and return it."""
    return make_users(data_dir)


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def valid_jwt(data_dir):
    """A valid JWT for ``admin`` signed with the fixed test secret.

    ``create_access_token`` reads ``security.jwt_secret`` from ``settings.yaml``
    at call time, so the file must exist first (``make_settings`` writes the
    deterministic test secret).
    """
    from app.auth.jwt_utils import create_access_token

    make_settings(data_dir)
    return create_access_token("admin")


@pytest.fixture
def auth_cookie(valid_jwt):
    """Cookie dict ``{"docky_token": <valid jwt>}``."""
    return {"docky_token": valid_jwt}


# ---------------------------------------------------------------------------
# Agent manager mocking
# ---------------------------------------------------------------------------

# Async methods that tests need to control on the mocked agent manager.
_ASYNC_METHODS = [
    "start_background_refresh",
    "ping_all",
    "ping_agent",
    "_request",
    "_stream_request",
    "_consume_stream",
    "get_containers",
    "get_stacks",
    "get_ports",
    "get_all_containers",
    "get_all_stacks",
    "get_all_ports",
    "get_cached_containers",
    "get_cached_stacks",
    "get_cached_ports",
    "refresh_cache",
    "refresh_all_caches",
    "invalidate_cache",
    "start_container",
    "stop_container",
    "restart_container",
    "start_stack",
    "stop_stack",
    "restart_stack",
    "deploy_stack",
    "update_stack",
    "get_container_logs",
    "get_container",
    "get_container_stats",
    "get_container_edit_spec",
    "update_container",
    "stream_update_container_image",
    "get_stack_files",
    "get_stack_files_with_content",
    "get_stack_file",
    "save_stack_file",
    "create_stack",
    "delete_stack",
    "set_permissions",
    "exec_container",
    "clean_agent",
    "get_stack_logs",
    "check_stack_update",
    "get_stack_history",
    "get_stack_version",
    "restore_stack_version",
    "update_git_history_settings",
    "stream_start_stack",
    "stream_stop_stack",
    "stream_restart_stack",
    "stream_update_stack",
    "stream_deploy_stack",
]


def _make_mock_agent_manager():
    """Build a MagicMock standing in for ``agent_manager``.

    ``agents`` / ``cache`` are real dicts so ``_resolve_agent`` and the
    settings CRUD / proxy endpoints behave predictably; every async method is
    an ``AsyncMock`` so ``await`` never blows up.
    """
    mm = MagicMock()
    mm.agents = {
        "Test Agent": {
            "url": "http://agent:8080",
            "api_key": "test-key",
            "status": "online",
            "last_check": 0,
        },
    }
    mm.cache = {}
    for name in _ASYNC_METHODS:
        setattr(mm, name, AsyncMock())
    return mm


@pytest.fixture
def mock_agent_manager(monkeypatch):
    """Replace ``agent_manager`` in ``app.routes.api`` and ``app.llm.client``.

    Both modules import the same global singleton; swapping the module-level
    name in each lets every endpoint/tool test drive the manager without
    touching the real (network-capable) singleton.
    """
    import app.routes.api as api_mod
    import app.llm.client as llm_mod

    mm = _make_mock_agent_manager()
    monkeypatch.setattr(api_mod, "agent_manager", mm)
    monkeypatch.setattr(llm_mod, "agent_manager", mm)
    return mm


@pytest.fixture
def mock_llm_client(monkeypatch):
    """Replace ``LLMClient`` in ``app.routes.api`` with a configurable fake."""
    import app.routes.api as api_mod

    fake = MagicMock()
    monkeypatch.setattr(api_mod, "LLMClient", lambda: fake)
    return fake


# ---------------------------------------------------------------------------
# TestClient fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def orchestrator_client(data_dir, monkeypatch):
    """A ``TestClient`` for the orchestrator app with a safe lifespan.

    The real ``startup_event`` calls ``agent_manager.start_background_refresh()``
    which opens network connections / spawns infinite loops — it is patched to a
    no-op ``AsyncMock`` before the lifespan runs. ``DOCKY_DATA_DIR`` already
    points to the session temp dir, and deterministic config files are seeded.
    No auth cookie is set by default.
    """
    from fastapi.testclient import TestClient

    from app.main import app
    from app.agent_manager.client import agent_manager

    make_settings(data_dir)
    make_users(data_dir)
    make_api_keys(data_dir)

    monkeypatch.setattr(agent_manager, "start_background_refresh", AsyncMock())

    with TestClient(app) as client:
        yield client


@pytest.fixture
def auth_client(orchestrator_client, auth_cookie):
    """``orchestrator_client`` pre-authenticated with a valid JWT cookie."""
    orchestrator_client.cookies.set("docky_token", auth_cookie["docky_token"])
    return orchestrator_client
