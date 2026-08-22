"""Dedicated CSRF tests (double-submit cookie — see docs/csrf-protection.md).

The WHOLE existing suite runs with CSRF disabled via the autouse
``disable_csrf_for_tests`` fixture in ``orchestrator/tests/conftest.py``
(env var ``DOCKY_DISABLE_CSRF_FOR_TESTS=1``). The tests below opt back IN
with the local ``csrf_on`` fixture, which deletes that variable.

Because autouse fixtures run before test-local ones at the same scope,
``csrf_on`` always executes after the conftest bypass has been applied →
the protection is effectively ON in every test of this module.

Covered behaviour:

- mutant /api/* request without header            → 403 {"detail": "CSRF"}
- wrong token                                     → 403
- matching cookie/header pair                     → endpoint reached
- safe methods never blocked                      → no 403 ever on GET
- POST /login protected (field or header missing) → redirect ?error=csrf
- POST /change-password protected                 → form re-rendered w/ message
- token rotated after successful login            → old token rejected
- constant-time comparison                        → functional + spy test
- security.csrf.enabled toggled at runtime        → late re-resolution
- page render issues the cookie + hidden field    → template wiring checked
"""

import re

import pytest
import yaml

from app.auth import csrf as csrf_mod

# Non-default credentials: a default-password account would trigger the
# forced rotation flow instead of the normal session (docs/password-rotation.md).
NON_DEFAULT_PASSWORD = "Csrf-Test-Pass1!"


def _seed_non_default_password(data_dir):
    """Re-seed users.yaml with admin/<NON_DEFAULT_PASSWORD> (~70 ms hash)."""
    from orchestrator.tests._helpers import make_users

    make_users(data_dir, password=NON_DEFAULT_PASSWORD)


def _set_csrf_pair(client, token: str):
    """Plant a csrf_token cookie and return the matching header dict."""
    client.cookies.set("csrf_token", token)
    return {"X-CSRF-Token": token}


@pytest.fixture
def csrf_on(monkeypatch):
    """Re-enable CSRF enforcement for this test (deletes the test bypass)."""
    monkeypatch.delenv(csrf_mod.TEST_BYPASS_ENV_VAR, raising=False)
    return csrf_mod


def _login_csrf_material(client):
    """GET /login and return (cookie_token, form_token) — must be equal."""
    resp = client.get("/login")
    assert resp.status_code == 200
    cookie_token = client.cookies.get(csrf_mod.CSRF_COOKIE_NAME)
    match = re.search(
        r'name="_csrf_token"\s+value="([^"]*)"', resp.text
    )
    assert match, "hidden _csrf_token field missing from login.html"
    assert cookie_token, "GET /login did not set the csrf_token cookie"
    assert cookie_token == match.group(1), "form field and cookie disagree"
    return cookie_token, match.group(1)


# ---------------------------------------------------------------------------
# Test-bypass mechanism itself (default state of every other test file)
# ---------------------------------------------------------------------------

def test_bypass_env_var_disables_protection(auth_client):
    """With DOCKY_DISABLE_CSRF_FOR_TESTS set (suite default): no 403, ever."""
    resp = auth_client.post("/api/presence/heartbeat")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


# ---------------------------------------------------------------------------
# /api/* mutants under enforcement
# ---------------------------------------------------------------------------

def test_api_mutant_without_header_is_403(auth_client, csrf_on):
    resp = auth_client.post("/api/presence/heartbeat")
    assert resp.status_code == 403
    assert resp.json() == {"detail": "CSRF"}


def test_api_mutant_with_wrong_token_is_403(auth_client, csrf_on):
    headers = _set_csrf_pair(auth_client, "valid-cookie-value")
    headers["X-CSRF-Token"] = "forged-value"
    resp = auth_client.post("/api/presence/heartbeat", headers=headers)
    assert resp.status_code == 403
    assert resp.json() == {"detail": "CSRF"}


def test_api_mutant_without_cookie_but_with_header_is_403(orchestrator_client, csrf_on):
    """Half a pair is not a pair: header alone cannot pass."""
    orchestrator_client.cookies.set("docky_token", "irrelevant-here")  # noqa: S105
    resp = orchestrator_client.post(
        "/api/presence/heartbeat", headers={"X-CSRF-Token": "some-token"}
    )
    # Auth would 401; CSRF denial (403) must win because it runs first.
    assert resp.status_code == 403


def test_api_mutant_with_matching_pair_reaches_endpoint(orchestrator_client, valid_jwt, csrf_on):
    orchestrator_client.cookies.set("docky_token", valid_jwt)
    headers = _set_csrf_pair(orchestrator_client, "pair-token")
    resp = orchestrator_client.post("/api/presence/heartbeat", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_safe_methods_are_never_blocked(orchestrator_client, csrf_on):
    """No cookies at all: GETs go through (auth answers 401/303, not 403)."""
    for path in ("/api/version", "/api/agents", "/dashboard", "/"):
        resp = orchestrator_client.get(path, follow_redirects=False)
        assert resp.status_code != 403, path


def test_unknown_non_api_mutant_not_in_scope(orchestrator_client, csrf_on):
    """Only /api/* is middleware-protected; other paths keep their semantics."""
    resp = orchestrator_client.post("/", follow_redirects=False)
    # Reaches routing (405 method-not-allowed), i.e. NOT blocked by CSRF.
    assert resp.status_code == 405


# ---------------------------------------------------------------------------
# Page renders issue the double-submit material
# ---------------------------------------------------------------------------

def test_login_page_sets_cookie_and_hidden_field(orchestrator_client, csrf_on):
    client = orchestrator_client
    resp = client.get("/login")
    set_cookie = resp.headers.get("set-cookie", "")
    assert f"{csrf_mod.CSRF_COOKIE_NAME}=" in set_cookie
    assert "samesite=lax" in set_cookie.lower()
    assert "httponly" not in set_cookie.lower()  # JS must read it
    _login_csrf_material(client)  # asserts field == cookie


def test_dashboard_page_sets_csrf_cookie(auth_client, csrf_on):
    resp = auth_client.get("/dashboard")
    assert resp.status_code == 200
    assert auth_client.cookies.get(csrf_mod.CSRF_COOKIE_NAME)


# ---------------------------------------------------------------------------
# POST /login protection
# ---------------------------------------------------------------------------

def test_login_without_token_redirects_to_error_csrf(orchestrator_client, data_dir, csrf_on):
    _seed_non_default_password(data_dir)
    resp = orchestrator_client.post(
        "/login",
        data={"username": "admin", "password": NON_DEFAULT_PASSWORD},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?error=csrf"


def test_login_with_wrong_token_redirects_to_error_csrf(orchestrator_client, data_dir, csrf_on):
    _seed_non_default_password(data_dir)
    resp = orchestrator_client.post(
        "/login",
        data={"username": "admin", "password": NON_DEFAULT_PASSWORD, "_csrf_token": "forged"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?error=csrf"


def test_login_with_valid_token_succeeds(orchestrator_client, data_dir, csrf_on):
    client = orchestrator_client
    _seed_non_default_password(data_dir)
    token, _ = _login_csrf_material(client)
    resp = client.post(
        "/login",
        data={
            "username": "admin",
            "password": NON_DEFAULT_PASSWORD,
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"


def test_login_accepts_header_instead_of_form_field(orchestrator_client, data_dir, csrf_on):
    """JS-driven submissions may send the header; forms use the hidden field."""
    client = orchestrator_client
    _seed_non_default_password(data_dir)
    token, _ = _login_csrf_material(client)
    resp = client.post(
        "/login",
        data={"username": "admin", "password": NON_DEFAULT_PASSWORD},
        headers={"X-CSRF-Token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"


def test_login_csrf_failure_does_not_count_as_rate_limit_failure(
    orchestrator_client, data_dir, csrf_on
):
    """A forged (CSRF-rejected) attempt must NOT consume rate-limit budget."""
    from app.auth.rate_limit import limiter

    _seed_non_default_password(data_dir)
    before = len(limiter._events.get("testclient", []))
    resp = orchestrator_client.post(
        "/login",
        data={"username": "admin", "password": NON_DEFAULT_PASSWORD},  # no token
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/login?error=csrf"
    after = len(limiter._events.get("testclient", []))
    assert after == before


# ---------------------------------------------------------------------------
# POST /change-password protection
# ---------------------------------------------------------------------------

def test_change_password_without_token_shows_error(orchestrator_client, csrf_on):
    client = orchestrator_client
    # Default-password login (with a valid CSRF proof) → restricted pwreset
    # cookie + change page.
    login_token, _ = _login_csrf_material(client)
    resp = client.post(
        "/login",
        data={"username": "admin", "password": "docky123", "_csrf_token": login_token},
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/change-password"
    assert client.cookies.get("docky_pwreset")

    resp = client.post(
        "/change-password",
        data={"new_password": "NewPass123!", "confirm_password": "NewPass123!"},
        follow_redirects=False,
    )
    assert resp.status_code == 200  # form re-rendered…
    assert "session expirée" in resp.text.lower()
    # …and nothing changed server-side (still awaiting rotation).
    assert client.cookies.get("docky_pwreset")


def test_change_password_with_valid_token_succeeds(orchestrator_client, csrf_on):
    client = orchestrator_client
    login_token, _ = _login_csrf_material(client)
    assert client.post(
        "/login",
        data={"username": "admin", "password": "docky123", "_csrf_token": login_token},
        follow_redirects=False,
    ).headers["location"] == "/change-password"

    resp = client.get("/change-password")
    match = re.search(r'name="_csrf_token"\s+value="([^"]*)"', resp.text)
    assert match
    token = match.group(1)

    resp = client.post(
        "/change-password",
        data={
            "new_password": "Brand-New-Pass-9!",
            "confirm_password": "Brand-New-Pass-9!",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"


# ---------------------------------------------------------------------------
# Post-authentication rotation
# ---------------------------------------------------------------------------

def test_token_rotated_after_login_old_token_rejected(orchestrator_client, data_dir, csrf_on):
    client = orchestrator_client
    _seed_non_default_password(data_dir)

    old_token, _ = _login_csrf_material(client)
    resp = client.post(
        "/login",
        data={
            "username": "admin",
            "password": NON_DEFAULT_PASSWORD,
            "_csrf_token": old_token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    new_token = client.cookies.get(csrf_mod.CSRF_COOKIE_NAME)
    assert new_token and new_token != old_token, "token was not rotated"

    # Old token against the NEW cookie → mismatch → rejected.
    resp = client.post(
        "/api/presence/heartbeat", headers={"X-CSRF-Token": old_token}
    )
    assert resp.status_code == 403

    # Fresh pair → accepted.
    resp = client.post(
        "/api/presence/heartbeat", headers={"X-CSRF-Token": new_token}
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Constant-time comparison
# ---------------------------------------------------------------------------

def test_constant_time_equals_functional():
    f = csrf_mod.constant_time_equals
    assert f("abc", "abc") is True
    assert f("a" * 64, "a" * 64) is True
    assert f("abc", "abd") is False
    assert f("abc", "abcd") is False          # length difference
    assert f("", "") is False                  # empty never matches
    assert f("", "x") is False
    assert f("x", "") is False


def test_verify_uses_constant_time_comparison(monkeypatch, orchestrator_client, csrf_on):
    """Spy on hmac.compare_digest to prove the code path goes through it."""
    calls = []
    real = csrf_mod.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(csrf_mod.hmac, "compare_digest", spy)

    orchestrator_client.cookies.set("csrf_token", "secret-value")  # noqa: S105

    # Direct functional check through a Request-like object.
    from fastapi import Request as FastAPIRequest

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/x",
        "headers": [
            (b"cookie", b"csrf_token=secret-value"),
            (b"x-csrf-token", b"secret-value"),
        ],
        "query_string": b"",
    }
    request = FastAPIRequest(scope)
    assert csrf_mod.verify_csrf(request) is True
    assert calls, "hmac.compare_digest was not used"


# ---------------------------------------------------------------------------
# Configuration toggle (late re-resolution, like the rate limiter)
# ---------------------------------------------------------------------------

def _merge_csrf_setting(data_dir, enabled: bool):
    settings_path = data_dir / "settings.yaml"
    settings = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
    settings.setdefault("security", {})["csrf"] = {"enabled": enabled}
    settings_path.write_text(
        yaml.safe_dump(settings, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def test_config_disabled_allows_mutations(orchestrator_client, valid_jwt, data_dir, csrf_on):
    """security.csrf.enabled=false short-circuits everything, re-read per call."""
    client = orchestrator_client
    client.cookies.set("docky_token", valid_jwt)

    _merge_csrf_setting(data_dir, enabled=False)
    resp = client.post("/api/presence/heartbeat")  # no CSRF material at all
    assert resp.status_code == 200

    _merge_csrf_setting(data_dir, enabled=True)
    resp = client.post("/api/presence/heartbeat")
    assert resp.status_code == 403
