"""CSRF protection for Docky (double-submit cookie, defense in depth).

See ``docs/csrf-protection.md`` for the full design. Summary:

- A random token is generated on every HTML page render and set as the
  ``csrf_token`` cookie (NON-httpOnly on purpose: the frontend JS must read
  it; ``samesite=lax``, ``path=/``, ``Secure`` when HTTPS is detected).
- Every mutating request (POST/PUT/PATCH/DELETE) under ``/api/*`` must carry
  an ``X-CSRF-Token`` header whose value equals the cookie; form endpoints
  (``POST /login``, ``POST /change-password``) accept the hidden field
  ``_csrf_token`` or the header instead.
- Comparison is constant-time (``hmac.compare_digest``).
- Configuration is re-read on every call (late resolution), consistent with
  the rate limiter and ``jwt_utils``:

.. code-block:: yaml

    security:
      csrf:
        enabled: true   # false disables the whole check

Test compatibility: the existing suite performs mutating requests without
any CSRF material. ``orchestrator/tests/conftest.py`` sets
``DOCKY_DISABLE_CSRF_FOR_TESTS=1`` via an autouse fixture; when present this
module short-circuits every check. It is a test-only escape hatch (same
philosophy as ``DOCKY_DATA_DIR``), not a production switch.
"""

import hmac
import os
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse

from app.config import get_setting

#: Cookie carrying the CSRF token (readable by JS → NOT httpOnly by design).
CSRF_COOKIE_NAME = "csrf_token"
#: Header the frontend must send on every mutating API call.
CSRF_HEADER_NAME = "X-CSRF-Token"
# Canonical lowercase key for Starlette's case-insensitive header lookups.
CSRF_HEADER_KEY = CSRF_HEADER_NAME.lower()
#: Hidden form field accepted by the classic HTML forms (login, change-password).
CSRF_FORM_FIELD = "_csrf_token"

#: Methods that never mutate state — never blocked.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

#: JSON API subtree protected by the middleware (header vs cookie only).
API_PROTECTED_PREFIX = "/api"
#: Form endpoints validated inside their handlers (body parsed by FastAPI),
#: deliberately skipped by the middleware to avoid consuming the raw body.
FORM_PROTECTED_PATHS = frozenset({"/login", "/change-password"})

#: Explicit middleware-level exemptions. Kept EMPTY on purpose: every /api/*
#: mutant is browser-issued (no server-to-server caller exists) — see
#: docs/csrf-protection.md §3. Add paths here only with a written rationale.
API_EXEMPT_PATHS: frozenset[str] = frozenset()

#: Lifetime of the csrf_token cookie (matches the session JWT, seconds).
CSRF_COOKIE_MAX_AGE = 86400

#: Test-only escape hatch (see module docstring + docs/csrf-protection.md §6).
TEST_BYPASS_ENV_VAR = "DOCKY_DISABLE_CSRF_FOR_TESTS"


# ---------------------------------------------------------------------------
# Configuration (late resolution, same pattern as rate_limit / jwt_utils)
# ---------------------------------------------------------------------------

def _as_bool(value, default: bool) -> bool:
    """Parse a loosely-typed YAML/env boolean, falling back to *default*."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _secure_override() -> bool | None:
    """Return the configured ``security.csrf.secure_cookie`` tri-state.

    ``None`` (default/absent) means auto-detect from the request; an explicit
    boolean forces the ``Secure`` attribute on/off (useful behind a reverse
    proxy that terminates TLS without forwarding ``X-Forwarded-Proto``).
    """
    raw = get_setting("security.csrf.secure_cookie")
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
    return None


def csrf_enabled() -> bool:
    """Whether CSRF verification is active for the current request.

    Resolution order:
      1. test bypass env var present → disabled (test suites only);
      2. ``security.csrf.enabled`` from settings.yaml → defaults to True
         (safe default, re-read on every call like the rate limiter).
    """
    if os.environ.get(TEST_BYPASS_ENV_VAR):
        return False
    return _as_bool(get_setting("security.csrf.enabled", True), True)


# ---------------------------------------------------------------------------
# Token lifecycle
# ---------------------------------------------------------------------------

def generate_csrf_token() -> str:
    """Return a fresh URL-safe random token (256 bits of entropy)."""
    return secrets.token_urlsafe(32)


def _request_is_https(request: Request) -> bool:
    """Best-effort HTTPS detection (scheme or standard proxy header)."""
    forwarded = request.headers.get("x-forwarded-proto", "")
    if forwarded.split(",")[0].strip().lower() == "https":
        return True
    try:
        return request.url.scheme == "https"
    except Exception:  # malformed scope — stay conservative, no Secure flag
        return False


def set_csrf_cookie(request: Request, response, token: str) -> str:
    """Set the ``csrf_token`` cookie on a page response; returns *token*.

    NON-httpOnly BY DESIGN (the frontend JS reads it to build the
    ``X-CSRF-Token`` header — double-submit pattern). ``Secure`` follows the
    auto-detected scheme unless ``security.csrf.secure_cookie`` overrides it.
    """
    override = _secure_override()
    secure = _request_is_https(request) if override is None else override
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=token,
        max_age=CSRF_COOKIE_MAX_AGE,
        path="/",
        httponly=False,
        samesite="lax",
        secure=secure,
    )
    return token


def rotate_csrf_cookie(request: Request, response) -> str:
    """Generate a fresh token AND set it as cookie (post-authentication rotation).

    Called after every successful authentication (login, forced password
    change): any token known before authentication becomes unusable.
    """
    return set_csrf_cookie(request, response, generate_csrf_token())


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def constant_time_equals(left: str, right: str) -> bool:
    """Constant-time string equality; empty/None values never match."""
    if not left or not right:
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def submitted_csrf_token(request: Request, form_value: str | None = None) -> str:
    """Extract the token supplied by the client for *request*.

    Priority: ``X-CSRF-Token`` header, then the ``_csrf_token`` form field
    (classic HTML form posts). Empty string when neither is present.
    """
    header = request.headers.get(CSRF_HEADER_KEY, "")
    if header:
        return header
    return form_value or ""


def verify_csrf(request: Request, form_value: str | None = None) -> bool:
    """True when the request carries a valid CSRF proof.

    Short-circuits to ``True`` when protection is disabled (config flag off
    or test bypass env var) so callers have a single choke point.
    """
    if not csrf_enabled():
        return True
    cookie_value = request.cookies.get(CSRF_COOKIE_NAME, "")
    supplied = submitted_csrf_token(request, form_value)
    return constant_time_equals(cookie_value, supplied)


def check_request_csrf(request: Request) -> JSONResponse | None:
    """Pure policy check used by the ASGI middleware.

    Returns a ready-to-send ``403`` JSONResponse when the request must be
    rejected, or ``None`` when it may proceed:

    - safe methods are always allowed;
    - form endpoints validate inside their handlers (body ownership);
    - non-/api paths are out of scope;
    - /api/* mutants need header == cookie.
    """
    if not csrf_enabled():
        return None
    if request.method.upper() in SAFE_METHODS:
        return None
    path = request.url.path
    if path in FORM_PROTECTED_PATHS:
        return None
    if not path.startswith(API_PROTECTED_PREFIX):
        return None
    if path in API_EXEMPT_PATHS:
        return None
    if verify_csrf(request):
        return None
    return JSONResponse(status_code=403, content={"detail": "CSRF"})


class CSRFMiddleware:
    """Pure ASGI middleware enforcing :func:`check_request_csrf`.

    Pure ASGI (not BaseHTTPMiddleware) so streaming responses (SSE) and
    WebSocket scopes flow through untouched — WebSockets have no CSRF-
    applicable handshake and are already authenticated via the session
    cookie (see docs/csrf-protection.md §4).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        denial = check_request_csrf(Request(scope))
        if denial is not None:
            await denial(scope, receive, send)
            return
        await self.app(scope, receive, send)
