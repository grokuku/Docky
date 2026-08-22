"""API endpoints for Docky (façade).

This module is the **façade** of the ``app.routes`` sub-package: it owns the
single ``APIRouter`` (``prefix="/api"``) used by ``app.main``, includes the
per-domain routers (agents, settings, containers, stacks, chat), injects the
agent-event broadcast callback into the ``agent_manager`` singleton, and
re-exports every shared symbol (helpers, ``agent_manager``, ``LLMClient``) so
the existing imports (``app.main``) and the tests' monkeypatches
(``app.routes.api.agent_manager`` / ``app.routes.api.LLMClient``) keep working
unchanged.

Internal routers resolve monkeypatch-sensitive symbols (``agent_manager``,
``LLMClient``) through this namespace at call time (``_api()``) so the
``app.routes.api.<symbole>`` patches from the tests keep taking effect
(pattern a — façade ré-export, identique aux refactors llm/agent_manager).
"""

import logging

logger = logging.getLogger(__name__)

from fastapi import APIRouter

from app.agent_manager.client import agent_manager
from app.routes.api_helpers import (
    _events_clients,
    _broadcast_agent_event,
    _find_version_path,
    _VERSION_PATH,
    _check_auth,
    _check_auth_ws,
    _unauthorized,
    _agent_bad_request,
    _agent_not_found,
    _agent_offline,
    _agent_unreachable,
    _resolve_agent,
    _check_agent_error,
    _sse_response,
    _sse_action_response,
    _mask_api_key,
)
from app.llm.client import (
    LLMClient,
    run_chat,
    read_soul,
    update_soul,
    execute_tool,
    build_system_prompt,
    TOOLS,
    HUMAN_VALIDATION_MARKER,
)
from app.routes import agents, settings, containers, stacks, chat
from app.routes.settings import _save_agents

router = APIRouter(prefix="/api")

router.include_router(agents.router)
router.include_router(settings.router)
router.include_router(containers.router)
router.include_router(stacks.router)
router.include_router(chat.router)

agent_manager.broadcast_agent_event = _broadcast_agent_event
