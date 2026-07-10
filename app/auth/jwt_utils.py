"""JWT utilities for Docky authentication."""

from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt

from app.config import get_setting


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
    """Verify *token* and return the username, or ``None`` if invalid."""
    try:
        payload = jwt.decode(
            token,
            _get_jwt_secret(),
            algorithms=[_get_jwt_algorithm()],
        )
        username: str | None = payload.get("sub")
        return username
    except (JWTError, Exception):
        return None