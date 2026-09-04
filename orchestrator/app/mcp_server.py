"""MCP (Model Context Protocol) server exposing Docky's tools to external LLMs.

Built on **FastMCP 4.x** (the standard high-level framework on top of the
official ``mcp`` SDK v2), this module reuses the existing 30 ``TOOLS`` and
``execute_tool`` from ``app.llm.tools`` so an external MCP client (Claude,
Cursor, any MCP-aware LLM) can drive containers, stacks, web search and the
persistent ``soul.md`` memory exactly like the built-in agentic chat loop.

Two transports are exposed:

- **Streamable HTTP** — mounted on the FastAPI app at ``/mcp`` (see
  ``app.main``). Secured with a Bearer API key configured via
  ``security.mcp_api_key`` in ``settings.yaml`` (auto-generated on first use).
- **stdio** — for local / CLI MCP clients: ``python -m app.mcp_server``.

Sensitive tools (``exec_in_container``, ``clean_agent``) are exposed but their
execution returns the ``HUMAN_VALIDATION_MARKER`` — the exact same behaviour
as the agentic chat loop — so no destructive command is ever run automatically
by an external LLM. See ``docs/mcp-server.md`` for the full design.
"""

import logging
import secrets
from typing import Any, Callable, Dict

from fastmcp import FastMCP
from fastmcp.tools import FunctionTool
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import get_setting, load_settings, save_settings
from app.llm.tools import TOOLS, execute_tool

logger = logging.getLogger(__name__)

# Mount point of the Streamable HTTP transport on the FastAPI app.
MCP_HTTP_PATH = "/mcp"


# ---------------------------------------------------------------------------
# API key management
# ---------------------------------------------------------------------------

def get_mcp_api_key() -> str:
    """Return the MCP Bearer API key, generating and persisting one if absent.

    The key lives under ``security.mcp_api_key`` in ``settings.yaml``. If it is
    missing (e.g. an existing deployment upgraded without the key), a new
    cryptographically-random key is generated and saved so the server stays
    secure out of the box.
    """
    settings = load_settings()
    key = settings.get("security", {}).get("mcp_api_key")
    if key:
        return key
    key = secrets.token_urlsafe(32)
    settings.setdefault("security", {})["mcp_api_key"] = key
    save_settings(settings)
    logger.info("Generated a new MCP API key (security.mcp_api_key).")
    return key


def mcp_auth_enabled() -> bool:
    """Whether the Bearer API key check is active (default: True)."""
    return bool(get_setting("security.mcp_enabled", True))


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def _make_tool_fn(tool_name: str) -> Callable[..., Any]:
    """Build the async ``**kwargs`` handler that delegates to ``execute_tool``.

    ``execute_tool`` is resolved through the ``app.llm.client`` façade at call
    time (see ``app.llm.tools._client``), so the ``mock_agent_manager`` /
    ``firecrawl_*`` monkeypatches used across the test-suite keep applying.
    """
    async def _call(**kwargs: Any) -> str:
        return await execute_tool(tool_name, kwargs)
    _call.__name__ = f"_mcp_{tool_name}"
    return _call


def _register_tools(mcp: FastMCP) -> None:
    """Register every tool from ``app.llm.tools.TOOLS`` on the FastMCP server."""
    for definition in TOOLS:
        fn = definition["function"]
        name = fn["name"]
        tool = FunctionTool(
            name=name,
            description=fn.get("description", ""),
            parameters=fn.get("parameters", {"type": "object", "properties": {}}),
            fn=_make_tool_fn(name),
        )
        mcp.add_tool(tool)


# ---------------------------------------------------------------------------
# Server construction (lazy singletons)
# ---------------------------------------------------------------------------

_mcp_server: FastMCP | None = None
_mcp_http_app: Any = None


def build_mcp_server() -> FastMCP:
    """Build (once) and return the FastMCP server with all Docky tools."""
    global _mcp_server
    if _mcp_server is None:
        mcp = FastMCP("Docky")
        _register_tools(mcp)
        _mcp_server = mcp
    return _mcp_server


# ---------------------------------------------------------------------------
# Bearer auth middleware
# ---------------------------------------------------------------------------

class _BearerAuthMiddleware:
    """Reject HTTP requests that do not carry the configured Bearer API key.

    The key is resolved lazily at request time so the middleware can be built
    before ``settings.yaml`` exists (it is created on startup). When
    ``security.mcp_enabled`` is False the check is skipped entirely.
    """

    def __init__(self, app: Any, get_key: Callable[[], str]):
        self.app = app
        self.get_key = get_key

    async def __call__(self, scope: Dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if not mcp_auth_enabled():
            return await self.app(scope, receive, send)

        request = Request(scope)
        expected = self.get_key()
        auth = request.headers.get("Authorization", "")
        if not expected or auth != f"Bearer {expected}":
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            return await response(scope, receive, send)
        return await self.app(scope, receive, send)


def get_mcp_http_app() -> Any:
    """Build (once) and return the Streamable HTTP sub-app (Starlette).

    The returned app is mounted on the FastAPI application at ``MCP_HTTP_PATH``.
    Its lifespan must be driven by the parent app (see ``get_mcp_lifespan``).
    """
    global _mcp_http_app
    if _mcp_http_app is None:
        _mcp_http_app = build_mcp_server().http_app(
            path="/",
            transport="streamable-http",
            stateless_http=True,
            middleware=[Middleware(_BearerAuthMiddleware, get_key=get_mcp_api_key)],
        )
    return _mcp_http_app


def get_mcp_lifespan(parent_app: Any) -> Any:
    """Return the sub-app's async context manager, driven by *parent_app*.

    FastAPI's ``app.mount`` does not run a mounted sub-app's lifespan, so the
    parent app must enter/exit it during its own startup/shutdown. This returns
    the ``async with`` context manager to be entered in the parent's startup
    and exited in its shutdown.
    """
    return get_mcp_http_app().lifespan(parent_app)


# ---------------------------------------------------------------------------
# stdio entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the MCP server over stdio (for local / CLI MCP clients)."""
    build_mcp_server().run(transport="stdio")


if __name__ == "__main__":
    main()
