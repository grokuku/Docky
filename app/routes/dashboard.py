"""Dashboard route (protected by JWT cookie)."""

from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import get_base_dir
from app.auth.router import COOKIE_NAME
from app.auth.jwt_utils import verify_token

router = APIRouter()
templates = Jinja2Templates(directory=str(get_base_dir() / "templates"))


def _is_authenticated(request: Request) -> Optional[str]:
    """Return the username if the request carries a valid JWT cookie."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return verify_token(token)


@router.get("/dashboard")
async def dashboard(request: Request):
    """Show the dashboard page, or redirect to login if not authenticated."""
    username = _is_authenticated(request)
    if username is None:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"username": username},
    )


@router.get("/settings")
async def settings_page(request: Request):
    """Show the settings page, or redirect to login if not authenticated."""
    username = _is_authenticated(request)
    if username is None:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {"username": username},
    )