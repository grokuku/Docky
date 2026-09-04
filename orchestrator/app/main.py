"""Docky FastAPI application entry point."""

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import ensure_config_files, get_base_dir, load_settings
from app.auth.router import router as auth_router
from app.auth.csrf import CSRFMiddleware
from app.routes.dashboard import router as dashboard_router
from app.routes.api import router as api_router
from app.agent_manager.client import agent_manager
from app.mcp_server import MCP_HTTP_PATH, get_mcp_http_app, get_mcp_lifespan
from app.version import get_version

# ---------------------------------------------------------------------------#
# App setup
# ---------------------------------------------------------------------------#

# Version résolue depuis version.txt (source de vérité du dépôt) — voir
# app/version.py et docs/versioning-unification.md.
app = FastAPI(title="Docky", version=get_version())

base_dir = get_base_dir()

# Static files
app.mount("/static", StaticFiles(directory=str(base_dir / "app" / "static")), name="static")

# Templates
templates = Jinja2Templates(directory=str(base_dir / "templates"))

# Routers
app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(api_router)

# MCP (Model Context Protocol) server — Streamable HTTP transport, secured
# with a Bearer API key (security.mcp_api_key). Voir docs/mcp-server.md.
# NB: ``app.mount`` ne pilote pas le lifespan du sous-app Starlette ; il est
# donc entré/sorti explicitement dans startup/shutdown ci-dessous. Le context
# manager est recréé à chaque startup (un objet ``async with`` ne se ré-entre
# pas), d'où la variable module ``_mcp_lifespan``.
_mcp_http_app = get_mcp_http_app()
app.mount(MCP_HTTP_PATH, _mcp_http_app, name="mcp")
_mcp_lifespan = None

# CSRF (double-submit cookie, défense en profondeur — voir
# docs/csrf-protection.md). Middleware ASGI pur : les scopes WebSocket et les
# réponses en streaming (SSE) traversent sans buffering. La vérification est
# relue tardivement à chaque requête (security.csrf.enabled) et court-circuitée
# par la variable d'environnement de test DOCKY_DISABLE_CSRF_FOR_TESTS.
app.add_middleware(CSRFMiddleware)


# ---------------------------------------------------------------------------#
# Startup
# ---------------------------------------------------------------------------#

@app.on_event("startup")
async def startup_event():
    """Ensure config files exist, then load settings on startup."""
    global _mcp_lifespan
    ensure_config_files()
    settings = load_settings()
    app.state.settings = settings

    # Démarre le lifespan du sous-app MCP (session manager) avant de servir
    # les requêtes Streamable HTTP.
    _mcp_lifespan = get_mcp_lifespan(app)
    await _mcp_lifespan.__aenter__()

    # Démarre la tâche de fond qui rafraîchit le cache des containers,
    # stacks et ports toutes les 5 secondes (stale-while-revalidate).
    asyncio.create_task(agent_manager.start_background_refresh())


@app.on_event("shutdown")
async def shutdown_event():
    """Ferme proprement le lifespan du sous-app MCP."""
    if _mcp_lifespan is not None:
        await _mcp_lifespan.__aexit__(None, None, None)


# ---------------------------------------------------------------------------#
# Root route
# ---------------------------------------------------------------------------#

@app.get("/")
async def root(request: Request):
    """Redirect to /dashboard if authenticated, otherwise to /login."""
    from app.auth.router import COOKIE_NAME
    from app.auth.jwt_utils import verify_token

    token = request.cookies.get(COOKIE_NAME)
    if token and verify_token(token):
        return RedirectResponse(url="/dashboard", status_code=303)
    return RedirectResponse(url="/login", status_code=303)