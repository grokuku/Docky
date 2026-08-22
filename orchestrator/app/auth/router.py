"""Authentication routes for Docky.

- ``GET/POST /login`` / ``GET /logout`` — classic session flow.
- ``GET/POST /change-password`` — forced rotation of the default bootstrap
  password (see ``app.auth.password_policy`` and ``docs/password-rotation.md``).

When a successful login detects an un-rotated password, NO full-session JWT
(``docky_token``) is issued: only a short-lived restricted token
(purpose="password_change", 10 min) stored in the ``docky_pwreset`` cookie,
which grants access to the password-change page alone.
"""

from typing import Optional

import bcrypt
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.config import find_user, load_users, save_users
from app.auth.csrf import (
    generate_csrf_token,
    rotate_csrf_cookie,
    set_csrf_cookie,
    verify_csrf,
)
from app.auth.jwt_utils import (
    create_access_token,
    create_password_change_token,
    verify_password_change_token,
)
from app.auth.password_policy import MIN_PASSWORD_LENGTH, rotation_required
from app.auth.rate_limit import (
    check_login_rate_limit,
    register_login_failure,
    register_login_success,
)

router = APIRouter()

COOKIE_NAME = "docky_token"
#: Cookie carrying the RESTRICTED password-change token (never a session).
PWRESET_COOKIE_NAME = "docky_pwreset"
#: Lifetime of the restricted cookie, matching the token expiry (seconds).
PWRESET_COOKIE_MAX_AGE = 600


@router.get("/login")
async def login_page(request: Request, error: Optional[str] = None):
    """Render the login page, optionally showing an error message.

    Each render also issues a fresh ``csrf_token`` cookie (double-submit
    pattern, see ``app.auth.csrf`` / docs/csrf-protection.md); the same value
    is embedded as the hidden ``_csrf_token`` form field.
    """
    from fastapi.templating import Jinja2Templates
    from app.config import get_base_dir

    templates = Jinja2Templates(directory=str(get_base_dir() / "templates"))
    csrf_token = generate_csrf_token()
    context = {"csrf_token": csrf_token}
    if error:
        context["error"] = error
    response = templates.TemplateResponse(request, "login.html", context)
    set_csrf_cookie(request, response, csrf_token)
    return response


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: Optional[str] = Form(None, alias="_csrf_token"),
):
    """Authenticate the user and set a JWT cookie on success.

    CSRF first (double-submit: hidden ``_csrf_token`` field vs cookie — see
    docs/csrf-protection.md): a rejected request redirects to the login page
    WITHOUT touching the rate limiter, so an attacker cannot use forged
    requests to lock a victim out. Failed attempts are then counted per
    client IP (sliding window, see ``app.auth.rate_limit``); once the
    threshold is reached the endpoint answers 429 until the window expires.
    A successful login resets the counter for that IP and rotates the CSRF
    token (post-authentication rotation).
    """
    if not verify_csrf(request, csrf_token):
        return RedirectResponse(url="/login?error=csrf", status_code=303)

    blocked = check_login_rate_limit(request)
    if blocked is not None:
        return blocked

    user = find_user(username)
    if user is None:
        register_login_failure(request)
        return RedirectResponse(url="/login?error=1", status_code=303)

    password_hash = user.get("password_hash", "")
    if not password_hash:
        register_login_failure(request)
        return RedirectResponse(url="/login?error=1", status_code=303)

    try:
        valid = bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        valid = False

    if not valid:
        register_login_failure(request)
        return RedirectResponse(url="/login?error=1", status_code=303)

    register_login_success(request)

    # Forced rotation: default/bootstrap password still in use → issue ONLY
    # the restricted password-change token (never a full session JWT) and
    # send the user to the dedicated page. The CSRF token is rotated in both
    # authenticated branches (post-authentication rotation).
    if rotation_required(user, password):
        reset_token = create_password_change_token(username)
        response = RedirectResponse(url="/change-password", status_code=303)
        response.set_cookie(
            key=PWRESET_COOKIE_NAME,
            value=reset_token,
            httponly=True,
            samesite="lax",
            max_age=PWRESET_COOKIE_MAX_AGE,
            path="/",
        )
        rotate_csrf_cookie(request, response)
        return response

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
    rotate_csrf_cookie(request, response)
    return response


@router.get("/logout")
async def logout():
    """Clear both auth cookies and redirect to the login page."""
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(key=COOKIE_NAME, path="/")
    response.delete_cookie(key=PWRESET_COOKIE_NAME, path="/")
    return response


# ---------------------------------------------------------------------------
# Forced password change (default password rotation)
# ---------------------------------------------------------------------------

def _templates():
    """Lazy Jinja2Templates (same directory as the other pages)."""
    from fastapi.templating import Jinja2Templates
    from app.config import get_base_dir

    return Jinja2Templates(directory=str(get_base_dir() / "templates"))


def _pwreset_username(request: Request) -> Optional[str]:
    """Return the username carried by a valid restricted cookie, else None."""
    token = request.cookies.get(PWRESET_COOKIE_NAME)
    if not token:
        return None
    return verify_password_change_token(token)


def _render_change_password(
    request: Request,
    error: Optional[str] = None,
    username: str = "",
    csrf_token: Optional[str] = None,
):
    """Render the forced password-change page (inline FR error messages).

    A fresh CSRF token is generated per render (and mirrored into the hidden
    form field) unless one is passed explicitly.
    """
    if csrf_token is None:
        csrf_token = generate_csrf_token()
    return _templates().TemplateResponse(
        request,
        "change_password.html",
        {"error": error, "username": username, "csrf_token": csrf_token},
    )


@router.get("/change-password")
async def change_password_page(request: Request):
    """Show the forced password-change form.

    Only reachable with a valid restricted ``docky_pwreset`` cookie; any
    other request is sent back to the login page. Each render issues a fresh
    ``csrf_token`` cookie + hidden field (double-submit pattern).
    """
    username = _pwreset_username(request)
    if username is None:
        return RedirectResponse(url="/login", status_code=303)
    response = _render_change_password(request, username=username)
    set_csrf_cookie(request, response, response.context["csrf_token"])
    return response


@router.post("/change-password")
async def change_password_submit(
    request: Request,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    csrf_token: Optional[str] = Form(None, alias="_csrf_token"),
):
    """Apply the forced password change, then open a normal session.

    CSRF first (hidden ``_csrf_token`` field vs cookie): a rejected request
    re-renders the form with an inline French message and changes nothing.
    Validation mirrors ``PUT /api/settings/password`` (min length) plus the
    confirmation and old-password checks; on error the form is re-rendered
    with an inline French message. On success the restricted cookie is
    cleared, the flag cleared in ``users.yaml``, and a full-session JWT is
    issued before redirecting to ``/dashboard``; the CSRF token is rotated.
    """
    username = _pwreset_username(request)
    if username is None:
        return RedirectResponse(url="/login", status_code=303)

    def _fail(message: str):
        return _render_change_password(request, error=message, username=username)

    if not verify_csrf(request, csrf_token):
        return _fail("Session expirée ou requête invalide. Rechargez la page et réessayez.")

    new_password = new_password or ""
    confirm_password = confirm_password or ""

    if not new_password or not confirm_password:
        return _fail("Veuillez renseigner le nouveau mot de passe et sa confirmation.")

    # Same minimum length as the settings endpoint.
    if len(new_password) < MIN_PASSWORD_LENGTH:
        return _fail(
            f"Mot de passe trop court : au moins {MIN_PASSWORD_LENGTH} caractères requis."
        )

    if new_password != confirm_password:
        return _fail("La confirmation ne correspond pas au nouveau mot de passe.")

    users_data = load_users()
    users_list = users_data.get("users", []) or []
    target = None
    for user in users_list:
        if user.get("username") == username:
            target = user
            break
    if target is None:
        # Account vanished meanwhile → restart the whole flow.
        response = RedirectResponse(url="/login?error=1", status_code=303)
        response.delete_cookie(key=PWRESET_COOKIE_NAME, path="/")
        return response

    stored_hash = target.get("password_hash", "")
    if stored_hash:
        try:
            same_as_old = bcrypt.checkpw(
                new_password.encode("utf-8"), stored_hash.encode("utf-8")
            )
        except (ValueError, TypeError):
            same_as_old = False
        if same_as_old:
            return _fail(
                "Le nouveau mot de passe doit être différent de l'ancien."
            )

    new_hash = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    target["password_hash"] = new_hash
    target["must_change_password"] = False
    save_users(users_data)

    # Rotation done: clear the restricted cookie, open a normal session and
    # rotate the CSRF token (post-authentication rotation).
    token = create_access_token(username)
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.delete_cookie(key=PWRESET_COOKIE_NAME, path="/")
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=86400,
        path="/",
    )
    rotate_csrf_cookie(request, response)
    return response