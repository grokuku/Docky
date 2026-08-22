"""JWT utilities for Docky authentication.

Two token scopes exist:

- **full session** (``create_access_token``) — no ``purpose`` claim; grants
  access to every protected page and ``/api/*`` endpoint;
- **password-change only** (``create_password_change_token``) — carries the
  dedicated ``purpose="password_change"`` claim and a short expiry (10 min).
  It is emitted by ``POST /login`` when the default password has not been
  rotated yet, and is ONLY accepted by ``verify_password_change_token``.
  ``verify_token`` rejects ANY token bearing a ``purpose`` claim, so a
  restricted token can never be used against the dashboard or the API.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt

from app.config import get_setting

#: Claim value marking a restricted, password-change-only token.
PASSWORD_CHANGE_PURPOSE = "password_change"

#: Lifetime of the restricted password-change token (minutes).
PASSWORD_CHANGE_EXPIRE_MINUTES = 10


def _get_jwt_secret() -> str:
    return get_setting("security.jwt_secret", "CHANGE_ME")


def _get_jwt_algorithm() -> str:
    return get_setting("security.jwt_algorithm", "HS256")


def _get_jwt_expire_minutes() -> int:
    return int(get_setting("security.jwt_expire_minutes", 1440))


def create_access_token(username: str) -> str:
    """Create a signed JWT for *username* with an expiration timestamp."""
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=_get_jwt_expire_minutes())
    payload = {
        "sub": username,
        "exp": expire,
        "iat": now,
    }
    return jwt.encode(payload, _get_jwt_secret(), algorithm=_get_jwt_algorithm())


def verify_token(token: str) -> Optional[str]:
    """Verify a FULL-SESSION *token* and return the username, else ``None``.

    Restricted tokens (bearing a ``purpose`` claim, e.g. the
    ``password_change`` scope) are deliberately rejected here so they cannot
    authenticate any dashboard page or ``/api/*`` endpoint. Use
    :func:`verify_password_change_token` for those.
    """
    try:
        payload = jwt.decode(
            token,
            _get_jwt_secret(),
            algorithms=[_get_jwt_algorithm()],
        )
        # Any scoped token is, by definition, not a full session token.
        if payload.get("purpose"):
            return None
        username: str | None = payload.get("sub")
        return username
    except (JWTError, Exception):
        return None


def create_password_change_token(username: str) -> str:
    """Create a SHORT-LIVED token that only allows the password change.

    The ``purpose`` claim scopes the token to ``POST /change-password``;
    its 10-minute expiry bounds the window in which the rotation must
    happen.
    """
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=PASSWORD_CHANGE_EXPIRE_MINUTES)
    payload = {
        "sub": username,
        "exp": expire,
        "iat": now,
        "purpose": PASSWORD_CHANGE_PURPOSE,
    }
    return jwt.encode(payload, _get_jwt_secret(), algorithm=_get_jwt_algorithm())


def verify_password_change_token(token: str) -> Optional[str]:
    """Verify a PASSWORD-CHANGE *token* and return the username, else ``None``.

    Mirrors :func:`verify_token`: signature, expiry and — crucially — the
    presence of the exact ``purpose="password_change"`` claim. A full
    session token is NOT accepted here.
    """
    try:
        payload = jwt.decode(
            token,
            _get_jwt_secret(),
            algorithms=[_get_jwt_algorithm()],
        )
        if payload.get("purpose") != PASSWORD_CHANGE_PURPOSE:
            return None
        username: str | None = payload.get("sub")
        return username or None
    except (JWTError, Exception):
        return None