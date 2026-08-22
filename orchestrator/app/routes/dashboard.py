"""Dashboard route (protected by JWT cookie)."""

from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import get_base_dir
from app.auth.router import COOKIE_NAME
from app.auth.csrf import generate_csrf_token, set_csrf_cookie
from app.auth.jwt_utils import verify_token

router = APIRouter()
templates = Jinja2Templates(directory=str(get_base_dir() / "templates"))


def _is_authenticated(request: Request) -> Optional[str]:
    """Return the username if the request carries a valid JWT cookie."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return verify_token(token)


def _render_page(request: Request, template_name: str, context: dict):
    """Render a protected HTML page with CSRF material (double-submit).

    Every HTML page render generates a fresh ``csrf_token``, embeds it in the
    template context AND sets it as a non-httpOnly cookie so the frontend JS
    can mirror it into the ``X-CSRF-Token`` header (see app.auth.csrf and
    docs/csrf-protection.md).
    """
    csrf_token = generate_csrf_token()
    context["csrf_token"] = csrf_token
    response = templates.TemplateResponse(request, template_name, context)
    set_csrf_cookie(request, response, csrf_token)
    return response


@router.get("/dashboard")
async def dashboard(request: Request):
    """Show the dashboard page, or redirect to login if not authenticated."""
    username = _is_authenticated(request)
    if username is None:
        return RedirectResponse(url="/login", status_code=303)
    return _render_page(request, "dashboard.html", {"username": username})


@router.get("/popup/logs")
async def popup_logs(request: Request, agent: str = "", container: str = "", name: str = "", stack: str = ""):
    """Popup page showing container or stack logs (separate window)."""
    username = _is_authenticated(request)
    if username is None:
        return RedirectResponse(url="/login", status_code=303)
    return _render_page(
        request,
        "logs.html",
        {"username": username, "agent": agent, "container": container, "name": name, "stack": stack},
    )


@router.get("/popup/console")
async def popup_console(request: Request, agent: str = "", container: str = "", name: str = ""):
    """Popup page for executing commands in a container (separate window)."""
    username = _is_authenticated(request)
    if username is None:
        return RedirectResponse(url="/login", status_code=303)
    return _render_page(
        request,
        "console.html",
        {"username": username, "agent": agent, "container": container, "name": name},
    )


@router.get("/settings")
async def settings_page(request: Request):
    """Show the settings page, or redirect to login if not authenticated."""
    username = _is_authenticated(request)
    if username is None:
        return RedirectResponse(url="/login", status_code=303)
    return _render_page(request, "settings.html", {"username": username})