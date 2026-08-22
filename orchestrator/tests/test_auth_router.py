"""Tests for the authentication router (login / logout).

Uses the pre-generated bcrypt hash of "docky123" — never re-hashes at test
time (except for the non-default password below, hashed once per module
import).

Note: since forced password rotation, logging in with the DEFAULT password
redirects to ``/change-password`` instead of ``/dashboard`` (see
docs/password-rotation.md). Tests exercising the NORMAL post-login flow
therefore seed a NON-default password via ``make_users(password=...)``.
"""

import bcrypt
import yaml

from orchestrator.tests._helpers import BCRYPT_DOCKY123  # noqa: F401 (garde test_bcrypt)

# Non-default credentials for tests that need the legacy login → dashboard
# flow (a default-password account is now sent to password rotation).
NON_DEFAULT_PASSWORD = "Adm1n-NonDefault!"
_NON_DEFAULT_HASH = bcrypt.hashpw(
    NON_DEFAULT_PASSWORD.encode("utf-8"), bcrypt.gensalt(12)
).decode()


def _seed_non_default_user(data_dir, username="admin"):
    """Overwrite users.yaml with an account whose password is NOT docky123."""
    return (data_dir / "users.yaml").write_text(
        yaml.safe_dump(
            {
                "users": [
                    {"username": username, "password_hash": _NON_DEFAULT_HASH},
                ],
            },
            default_flow_style=False,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_login_ok_sets_cookie(orchestrator_client, data_dir):
    client = orchestrator_client
    # Non-default password: the seeded admin/docky123 account would trigger
    # the forced rotation flow instead of the normal session.
    _seed_non_default_user(data_dir)
    resp = client.post(
        "/login",
        data={"username": "admin", "password": NON_DEFAULT_PASSWORD},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"

    set_cookie = resp.headers.get("set-cookie", "")
    assert "docky_token=" in set_cookie
    assert "httponly" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()
    assert client.cookies.get("docky_token")


def test_login_wrong_password_redirects_error(orchestrator_client):
    client = orchestrator_client
    resp = client.post(
        "/login", data={"username": "admin", "password": "wrong"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?error=1"
    assert "set-cookie" not in resp.headers or "docky_token=" not in resp.headers.get("set-cookie", "")
    assert client.cookies.get("docky_token") is None


def test_login_unknown_user_redirects_error(orchestrator_client):
    client = orchestrator_client
    resp = client.post(
        "/login", data={"username": "ghost", "password": "docky123"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?error=1"
    assert client.cookies.get("docky_token") is None


def test_login_empty_password_hash_redirects_error(orchestrator_client, data_dir):
    client = orchestrator_client
    # Overwrite users.yaml with a user whose password_hash is empty.
    users_path = data_dir / "users.yaml"
    users_path.write_text(
        yaml.safe_dump(
            {"users": [{"username": "admin", "password_hash": ""}]},
            default_flow_style=False,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    resp = client.post(
        "/login", data={"username": "admin", "password": "docky123"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?error=1"
    assert client.cookies.get("docky_token") is None


def test_login_page_returns_html(orchestrator_client):
    client = orchestrator_client
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert resp.text.strip() != ""


def test_logout_clears_cookie(orchestrator_client, data_dir):
    client = orchestrator_client
    # Non-default password (see module docstring): normal session flow.
    _seed_non_default_user(data_dir)
    # Log in first so a cookie exists.
    resp = client.post(
        "/login",
        data={"username": "admin", "password": NON_DEFAULT_PASSWORD},
        follow_redirects=False,
    )
    assert client.cookies.get("docky_token")

    resp = client.get("/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    # The Set-Cookie header expires / clears the token.
    set_cookie = resp.headers.get("set-cookie", "").lower()
    assert "docky_token=" in set_cookie
    assert client.cookies.get("docky_token") is None


def test_bcrypt_hash_matches_expected_password():
    """Guard: the pre-generated hash really is the one for 'docky123'."""
    import bcrypt

    assert bcrypt.checkpw(b"docky123", BCRYPT_DOCKY123.encode("utf-8"))
