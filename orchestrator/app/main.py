"""Docky FastAPI application entry point."""

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import ensure_config_files, get_base_dir, load_settings
from app.auth.router import router as auth_router
from app.routes.dashboard import router as dashboard_router
from app.routes.api import router as api_router
from app.agent_manager.client import agent_manager

# ---------------------------------------------------------------------------#
# App setup
# ---------------------------------------------------------------------------#

app = FastAPI(title="Docky", version="0.1.0")

base_dir = get_base_dir()

# Static files
app.mount("/static", StaticFiles(directory=str(base_dir / "app" / "static")), name="static")

# Templates
templates = Jinja2Templates(directory=str(base_dir / "templates"))

# Routers
app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(api_router)


# ---------------------------------------------------------------------------#
# Startup
# ---------------------------------------------------------------------------#

@app.on_event("startup")
async def startup_event():
    """Ensure config files exist, then load settings on startup."""
    ensure_config_files()
    settings = load_settings()
    app.state.settings = settings

    # Démarre la tâche de fond qui rafraîchit le cache des containers,
    # stacks et ports toutes les 5 secondes (stale-while-revalidate).
    asyncio.create_task(agent_manager.start_background_refresh())


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