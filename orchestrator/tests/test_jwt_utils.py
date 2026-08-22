"""Tests for ``app.auth.jwt_utils``.

JWT creation/verification is driven by ``security.*`` values read from
``settings.yaml`` (which the root conftest points at a session temp dir with
a fixed secret).
"""

from datetime import datetime, timedelta, timezone

import pytest
from jose import jwt

from orchestrator.tests._helpers import make_settings


@pytest.fixture(autouse=True)
def _jwt_settings(data_dir):
    """Ensure ``settings.yaml`` has the deterministic secret for every test."""
    make_settings(data_dir)


def _secret():
    from app.config import get_setting

    return get_setting("security.jwt_secret")


# ---------------------------------------------------------------------------
# create_access_token
# ---------------------------------------------------------------------------

def test_create_access_token_returns_decodable_string(data_dir):
    from app.auth.jwt_utils import create_access_token

    token = create_access_token("admin")
    assert isinstance(token, str) and len(token) > 20

    payload = jwt.decode(token, _secret(), algorithms=["HS256"])
    assert payload["sub"] == "admin"


def test_create_access_token_sub_is_username(data_dir):
    from app.auth.jwt_utils import create_access_token

    payload = jwt.decode(create_access_token("bob"), _secret(), algorithms=["HS256"])
    assert payload["sub"] == "bob"


def test_create_access_token_exp_in_future(data_dir):
    from app.auth.jwt_utils import create_access_token

    payload = jwt.decode(create_access_token("admin"), _secret(), algorithms=["HS256"])
    assert payload["exp"] > datetime.now(timezone.utc).timestamp()


def test_create_access_token_has_iat(data_dir):
    from app.auth.jwt_utils import create_access_token

    payload = jwt.decode(create_access_token("admin"), _secret(), algorithms=["HS256"])
    assert "iat" in payload
    assert isinstance(payload["iat"], (int, float))


# ---------------------------------------------------------------------------
# verify_token
# ---------------------------------------------------------------------------

def test_verify_token_valid_returns_username(data_dir):
    from app.auth.jwt_utils import create_access_token, verify_token

    token = create_access_token("admin")
    assert verify_token(token) == "admin"


def test_verify_token_garbage_returns_none(data_dir):
    from app.auth.jwt_utils import verify_token

    assert verify_token("garbage-token") is None
    assert verify_token("") is None


def test_verify_token_wrong_secret_returns_none(data_dir):
    from app.auth.jwt_utils import create_access_token, verify_token

    token = jwt.encode(
        {"sub": "admin", "exp": datetime.now(timezone.utc) + timedelta(minutes=10)},
        "a-completely-different-secret",
        algorithm="HS256",
    )
    assert verify_token(token) is None


def test_verify_token_expired_returns_none(data_dir):
    from app.auth.jwt_utils import verify_token

    expired = jwt.encode(
        {
            "sub": "admin",
            "exp": datetime.now(timezone.utc) - timedelta(minutes=5),
        },
        _secret(),
        algorithm="HS256",
    )
    assert verify_token(expired) is None


# ---------------------------------------------------------------------------
# security.* settings are read from settings.yaml
# ---------------------------------------------------------------------------

def test_security_settings_read_from_settings_yaml(data_dir):
    from app.config import get_setting

    make_settings(data_dir, jwt_secret="custom-fixed-secret")
    assert get_setting("security.jwt_secret") == "custom-fixed-secret"
    assert get_setting("security.jwt_algorithm") == "HS256"
    assert get_setting("security.jwt_expire_minutes") == 1440


def test_create_access_token_uses_settings_secret(data_dir):
    from app.auth.jwt_utils import create_access_token

    make_settings(data_dir, jwt_secret="settings-driven-secret")
    token = create_access_token("admin")
    # Decoding with the settings secret succeeds...
    payload = jwt.decode(token, "settings-driven-secret", algorithms=["HS256"])
    assert payload["sub"] == "admin"
    # ...while the default secret does not.
    with pytest.raises(Exception):
        jwt.decode(token, "test-jwt-secret", algorithms=["HS256"])
