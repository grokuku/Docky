"""Tests for the login rate limiter (``app.auth.rate_limit``).

The limiter is an in-memory, process-wide singleton, so an autouse fixture
resets it around every test to keep the suite order-independent. Low
thresholds under test are injected by rewriting ``settings.yaml`` — the
limiter re-reads the config at each request (late resolution), exactly like
``jwt_utils``. No existing test infra is modified: the seeded
``make_settings`` has no ``rate_limit`` key, so existing tests run with the
permissive defaults (5 failures / 300 s) and never trip the limiter.
"""

import pytest
import yaml

from app.auth import rate_limit as rl
from orchestrator.tests._helpers import make_users

# Non-default password for tests exercising the SUCCESSFUL login path: since
# forced password rotation, a default-password (docky123) account is
# redirected to /change-password instead of /dashboard (see
# docs/password-rotation.md). These tests assert the legacy redirect, so they
# seed an explicit non-default password.
NON_DEFAULT_PASSWORD = "Rate-Limit-Pass1!"


def _seed_non_default_password(data_dir):
    """Re-seed users.yaml with admin/<NON_DEFAULT_PASSWORD> (~70 ms hash)."""
    make_users(data_dir, password=NON_DEFAULT_PASSWORD)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_rate_limiter():
    """Isolate the process-wide limiter state around each test."""
    rl.reset_rate_limiter()
    yield
    rl.reset_rate_limiter()


def _write_rate_limit_settings(data_dir, **overrides):
    """Merge a ``security.rate_limit`` section into the seeded settings.yaml.

    Must be called AFTER the ``orchestrator_client`` fixture has seeded the
    deterministic settings (it would otherwise overwrite them).
    """
    settings_path = data_dir / "settings.yaml"
    settings = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
    section = {
        "enabled": True,
        "max_attempts": 3,
        "window_seconds": 300,
        "trust_proxy": False,
    }
    section.update(overrides)
    settings.setdefault("security", {})["rate_limit"] = section
    settings_path.write_text(
        yaml.safe_dump(settings, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def _post_login(client, username="admin", password="wrong", headers=None):
    return client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
        headers=headers or {},
    )


class _FakeClock:
    """Controllable replacement for ``rate_limit.monotonic``."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# Behaviour under the threshold / at the threshold
# ---------------------------------------------------------------------------

def test_failures_under_threshold_behave_normally(orchestrator_client, data_dir):
    """Below the threshold, responses are byte-identical to the legacy flow."""
    client = orchestrator_client
    _write_rate_limit_settings(data_dir, max_attempts=3)
    _seed_non_default_password(data_dir)

    for _ in range(2):
        resp = _post_login(client)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login?error=1"

    # Correct credentials still work right up to the threshold.
    resp = _post_login(client, password=NON_DEFAULT_PASSWORD)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"


def test_threshold_reached_returns_429(orchestrator_client, data_dir):
    client = orchestrator_client
    _write_rate_limit_settings(data_dir, max_attempts=2)

    assert _post_login(client).status_code == 303  # failure 1
    assert _post_login(client).status_code == 303  # failure 2 → threshold

    # Even CORRECT credentials are now blocked, with 429 + Retry-After.
    resp = _post_login(client, password="docky123")
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
    assert int(resp.headers["Retry-After"]) >= 1
    assert "set-cookie" not in resp.headers
    assert "Trop de tentatives" in resp.text


def test_blocked_ip_cannot_extend_block_by_retrying(orchestrator_client, data_dir, monkeypatch):
    """Hammering while blocked must not re-record failures (time-based unblock)."""
    client = orchestrator_client
    _write_rate_limit_settings(data_dir, max_attempts=1, window_seconds=100)

    clock = _FakeClock()
    monkeypatch.setattr(rl, "monotonic", clock)

    assert _post_login(client).status_code == 303  # the single allowed failure (t=1000)
    for i in range(10):
        clock.advance(1)  # t=1001..1010: blocked attempts must NOT be recorded
        assert _post_login(client).status_code == 429

    clock.advance(90)  # t=1100 = first failure + window → block MUST be over
    # (had the blocked requests been recorded, expiry would be t=1110)
    assert _post_login(client).status_code == 303  # unblocked, wrong creds


def test_get_login_not_rate_limited(orchestrator_client, data_dir):
    """Only POST /login is limited; the page itself stays reachable."""
    client = orchestrator_client
    _write_rate_limit_settings(data_dir, max_attempts=1)

    assert _post_login(client).status_code == 303
    assert _post_login(client).status_code == 429

    resp = client.get("/login")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


# ---------------------------------------------------------------------------
# Counter reset on success
# ---------------------------------------------------------------------------

def test_success_resets_counter(orchestrator_client, data_dir):
    """A successful login wipes the IP's counter (no lock-out for legit users)."""
    client = orchestrator_client
    _write_rate_limit_settings(data_dir, max_attempts=2)
    _seed_non_default_password(data_dir)

    assert _post_login(client).status_code == 303  # failure 1
    resp = _post_login(client, password=NON_DEFAULT_PASSWORD)  # success → reset
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"

    # Without the reset, this next failure would already trip max_attempts=2.
    assert _post_login(client).status_code == 303  # failure 1 again
    assert _post_login(client).status_code == 303  # failure 2
    assert _post_login(client, password=NON_DEFAULT_PASSWORD).status_code == 429


# ---------------------------------------------------------------------------
# Window expiry
# ---------------------------------------------------------------------------

def test_window_expiry_unblocks(orchestrator_client, data_dir, monkeypatch):
    """After window_seconds, the failures age out and the IP is unblocked."""
    client = orchestrator_client
    _write_rate_limit_settings(data_dir, max_attempts=1, window_seconds=50)

    clock = _FakeClock()
    monkeypatch.setattr(rl, "monotonic", clock)

    assert _post_login(client).status_code == 303  # failure at t=1000
    assert _post_login(client).status_code == 429  # blocked

    clock.advance(49)
    assert _post_login(client).status_code == 429  # still inside the window

    clock.advance(2)  # t = 1051 > 1000 + 50 → window expired
    assert _post_login(client).status_code == 303  # unblocked (wrong creds)


# ---------------------------------------------------------------------------
# Per-IP keys / X-Forwarded-For
# ---------------------------------------------------------------------------

def test_distinct_ips_are_independent(orchestrator_client, data_dir):
    """Blocking one IP never affects another (trust_proxy enabled)."""
    client = orchestrator_client
    _write_rate_limit_settings(data_dir, max_attempts=1, trust_proxy=True)

    assert (
        _post_login(client, headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 303
    )
    # 1.1.1.1 is now blocked — even with correct credentials…
    assert (
        _post_login(client, password="docky123", headers={"X-Forwarded-For": "1.1.1.1"})
        .status_code
        == 429
    )
    # …while 2.2.2.2 is untouched.
    _seed_non_default_password(data_dir)
    resp = _post_login(client, password=NON_DEFAULT_PASSWORD, headers={"X-Forwarded-For": "2.2.2.2"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"


def test_forwarded_for_ignored_when_trust_proxy_disabled(orchestrator_client, data_dir):
    """Without trust_proxy, X-Forwarded-For cannot be used to evade the block."""
    client = orchestrator_client
    _write_rate_limit_settings(data_dir, max_attempts=1, trust_proxy=False)

    # Attacker spoofs a fresh IP on every request.
    assert (
        _post_login(client, headers={"X-Forwarded-For": "9.9.9.9"}).status_code == 303
    )
    assert (
        _post_login(client, headers={"X-Forwarded-For": "8.8.8.8"}).status_code == 429
    )
    # Key is the socket peer ("testclient"), not the spoofed header.
    assert rl.get_client_key(
        _build_request(orchestrator_client, {"X-Forwarded-For": "7.7.7.7"}),
        rl.RateLimitConfig(trust_proxy=False),
    ) == "testclient"


def test_forwarded_for_first_ip_used_when_trusted(orchestrator_client, data_dir):
    """With trust_proxy, only the FIRST X-Forwarded-For entry is the key."""
    client = orchestrator_client
    _write_rate_limit_settings(data_dir, max_attempts=1, trust_proxy=True)

    assert (
        _post_login(
            client, headers={"X-Forwarded-For": "5.5.5.5, 10.0.0.1"}
        ).status_code
        == 303
    )
    assert (
        _post_login(
            client, password="docky123", headers={"X-Forwarded-For": "5.5.5.5, 10.0.0.1"}
        ).status_code
        == 429
    )
    # 10.0.0.1 (second entry) is not the key → unaffected.
    _seed_non_default_password(data_dir)
    resp = _post_login(
        client, password=NON_DEFAULT_PASSWORD, headers={"X-Forwarded-For": "10.0.0.1"}
    )
    assert resp.status_code == 303
    assert resp.headers.get("location") == "/dashboard"


def _build_request(client, headers):
    """Build a real Request for direct get_client_key assertions."""
    from starlette.requests import Request as StarletteRequest

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/login",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "query_string": b"",
        "client": ("testclient", 50000),
    }
    return StarletteRequest(scope)


# ---------------------------------------------------------------------------
# Config handling
# ---------------------------------------------------------------------------

def test_disabled_limiter_never_blocks(orchestrator_client, data_dir):
    client = orchestrator_client
    _write_rate_limit_settings(data_dir, enabled=False, max_attempts=1)
    _seed_non_default_password(data_dir)

    for _ in range(5):
        assert _post_login(client).status_code == 303
    resp = _post_login(client, password=NON_DEFAULT_PASSWORD)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"


def test_config_invalid_values_fall_back_to_defaults(monkeypatch):
    """Garbage config values must not crash or produce absurd limits."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "settings.yaml").write_text(
            yaml.safe_dump(
                {
                    "security": {
                        "rate_limit": {
                            "enabled": "no",
                            "max_attempts": "abc",
                            "window_seconds": -5,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("DOCKY_DATA_DIR", tmp)
        cfg = rl.RateLimitConfig.from_settings()

    assert cfg.enabled is False  # "no" parses as falsy
    assert cfg.max_attempts == 5  # invalid → default
    assert cfg.window_seconds == 300  # invalid → default
    assert cfg.trust_proxy is False  # absent → default


def test_missing_rate_limit_section_uses_safe_defaults(monkeypatch):
    """No ``rate_limit`` key at all → enabled with documented defaults."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "settings.yaml").write_text(
            yaml.safe_dump({"security": {"jwt_secret": "x"}}), encoding="utf-8"
        )
        monkeypatch.setenv("DOCKY_DATA_DIR", tmp)
        cfg = rl.RateLimitConfig.from_settings()

    assert cfg == rl.RateLimitConfig(
        enabled=True, max_attempts=5, window_seconds=300, trust_proxy=False
    )
