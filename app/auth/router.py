"""Authentication routes for Docky (login / logout)."""

from typing import Optional

import bcrypt
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.config import find_user
from app.auth.jwt_utils import create_access_token

router = APIRouter()

COOKIE_NAME = "docky_token"


@router.get("/login")
async def login_page(request: Request, error: Optional[str] = None):
    """Render the login page, optionally showing an error message."""
    from fastapi.templating import Jinja2Templates
    from app.config import get_base_dir

    templates = Jinja2Templates(directory=str(get_base_dir() / "templates"))
    context = {}
    if error:
        context["error"] = error
    return templates.TemplateResponse(request, "login.html", context)


@router.post("/login")
async def login_submit(
    username: str = Form(...),
    password: str = Form(...),
):
    """Authenticate the user and set a JWT cookie on success."""
    user = find_user(username)
    if user is None:
        return RedirectResponse(url="/login?error=1", status_code=303)

    password_hash = user.get("password_hash", "")
    if not password_hash:
        return RedirectResponse(url="/login?error=1", status_code=303)

    try:
        valid = bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        valid = False

    if not valid:
        return RedirectResponse(url="/login?error=1", status_code=303)

    token = create_access_token(username)
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=86400,
        path="/",
    )
    return response


@router.get("/logout")
async def logout():
    """Clear the JWT cookie and redirect to the login page."""
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return response