"""In-memory rate limiting for the login endpoint (anti brute-force).

Pure stdlib implementation — no new dependency. Designed for the standard
Docky deployment: a single uvicorn process (async). A plain
``threading.Lock`` guards the store so the module also stays correct if
the app is ever run multi-threaded; note that with multiple *processes*
each worker keeps its own counters (inherent to any in-memory limiter,
see docs/rate-limiting.md).

Algorithm: sliding window over **failed** login attempts per client key.
A successful login resets the counter for that IP. While blocked, requests
are rejected (429) before credentials are checked and do NOT extend the
block: unblocking is purely time-based (when the oldest failure in the
window ages out).

Client key = direct socket IP (``request.client.host``). The
``X-Forwarded-For`` header (first IP) is honoured only when
``security.rate_limit.trust_proxy`` is enabled, so direct clients cannot
spoof it.

Configuration is re-read on every call (late resolution via
``app.config.get_setting``), consistent with ``jwt_utils``:

.. code-block:: yaml

    security:
      rate_limit:
        enabled: true         # false disables the limiter entirely
        max_attempts: 5       # failures per window before blocking
        window_seconds: 300   # sliding window size
        trust_proxy: false    # honour X-Forwarded-For (behind reverse proxy)
"""

import threading
from collections import deque
from dataclasses import dataclass
from time import monotonic

from fastapi import Request
from fastapi.responses import HTMLResponse

from app.config import get_setting

# Bound the memory used by the store: sweep fully-expired keys whenever
# more than this many IPs are tracked, evicting oldest entries as a last
# resort (dicts preserve insertion order).
MAX_TRACKED_CLIENTS = 4096

_DEFAULT_MAX_ATTEMPTS = 5
_DEFAULT_WINDOW_SECONDS = 300


@dataclass(frozen=True)
class RateLimitConfig:
    """Resolved rate-limit settings (see module docstring)."""

    enabled: bool = True
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS
    window_seconds: int = _DEFAULT_WINDOW_SECONDS
    trust_proxy: bool = False

    @classmethod
    def from_settings(cls) -> "RateLimitConfig":
        """Build a config from ``settings.yaml``, falling back to safe defaults."""
        raw = get_setting("security.rate_limit", {}) or {}
        if not isinstance(raw, dict):
            raw = {}
        return cls(
            enabled=_as_bool(raw.get("enabled"), True),
            max_attempts=_as_positive_int(raw.get("max_attempts"), _DEFAULT_MAX_ATTEMPTS),
            window_seconds=_as_positive_int(
                raw.get("window_seconds"), _DEFAULT_WINDOW_SECONDS
            ),
            trust_proxy=_as_bool(raw.get("trust_proxy"), False),
        )


def _as_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _as_positive_int(value, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


class LoginRateLimiter:
    """Sliding-window store of failed login timestamps, keyed by client IP."""

    def __init__(self):
        # key -> deque of monotonic timestamps of recent failures.
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def blocked_for(self, key: str, cfg: RateLimitConfig) -> float:
        """Return seconds remaining until *key* is unblocked (0.0 = not blocked)."""
        now = monotonic()
        cutoff = now - cfg.window_seconds
        with self._lock:
            window = self._events.get(key)
            if not window:
                return 0.0
            self._prune(window, cutoff)
            if len(window) < cfg.max_attempts:
                return 0.0
            oldest = window[0]
            remaining = (oldest + cfg.window_seconds) - now
            return max(remaining, 0.0)

    def record_failure(self, key: str, cfg: RateLimitConfig) -> None:
        """Register a failed attempt for *key* and keep memory bounded."""
        now = monotonic()
        cutoff = now - cfg.window_seconds
        with self._lock:
            window = self._events.get(key)
            if window is None:
                window = deque()
                self._events[key] = window
            self._prune(window, cutoff)
            window.append(now)
            if len(self._events) > MAX_TRACKED_CLIENTS:
                self._sweep(cutoff)

    def record_success(self, key: str) -> None:
        """A successful login wipes the counter for *key*."""
        with self._lock:
            self._events.pop(key, None)

    def reset(self) -> None:
        """Drop all state (used by the test-suite between tests)."""
        with self._lock:
            self._events.clear()

    # ------------------------------------------------------------------
    # Internals — must be called while holding ``self._lock``
    # ------------------------------------------------------------------

    @staticmethod
    def _prune(window: deque, cutoff: float) -> None:
        """Drop timestamps older than *cutoff* from the left (deque is ordered)."""
        while window and window[0] <= cutoff:
            window.popleft()

    def _sweep(self, cutoff: float) -> None:
        """Remove fully-expired keys; evict oldest-inserted ones if still full."""
        expired = [key for key, window in self._events.items() if not window or window[0] <= cutoff]
        for key in expired:
            del self._events[key]
        while len(self._events) > MAX_TRACKED_CLIENTS:
            # dict preserves insertion order → pop the least-recently-seen key.
            self._events.pop(next(iter(self._events)))


# Module-level singleton shared by the auth router; tests reset it via
# :func:`reset_rate_limiter`.
limiter = LoginRateLimiter()


def reset_rate_limiter() -> None:
    """Clear all rate-limit state (test helper)."""
    limiter.reset()


# ----------------------------------------------------------------------
# Request-facing helpers
# ----------------------------------------------------------------------

def get_client_key(request: Request, cfg: RateLimitConfig) -> str:
    """Resolve the client key for *request*.

    Honours the first IP of ``X-Forwarded-For`` only when *cfg* enables
    ``trust_proxy`` (otherwise the header could be spoofed by direct
    clients). Falls back to the socket peer, then to ``"unknown"``.
    """
    if cfg.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            first_ip = forwarded.split(",")[0].strip()
            if first_ip:
                return first_ip
    client = request.client
    return client.host if client else "unknown"


_RATE_LIMIT_BODY = (
    "<!DOCTYPE html>"
    '<html lang="fr"><head><meta charset="UTF-8">'
    "<title>Docky - Trop de tentatives</title></head>"
    '<body style="font-family:sans-serif;background:#12141a;color:#e6e6e6;'
    'display:flex;align-items:center;justify-content:center;height:100vh;margin:0;">'
    "<div style=\"text-align:center\">"
    "<h1>429 &mdash; Trop de tentatives</h1>"
    "<p>Trop de tentatives de connexion depuis cette adresse.<br>"
    "Réessayez dans quelques minutes.</p>"
    '<a href="/login" style="color:#7aa2f7">Retour à la page de connexion</a>'
    "</div></body></html>"
)


def check_login_rate_limit(request: Request) -> HTMLResponse | None:
    """Return a 429 response if the client is blocked, else ``None``.

    Disabled config short-circuits everything (no state touched).
    """
    cfg = RateLimitConfig.from_settings()
    if not cfg.enabled:
        return None
    remaining = limiter.blocked_for(get_client_key(request, cfg), cfg)
    if remaining <= 0.0:
        return None
    return HTMLResponse(
        content=_RATE_LIMIT_BODY,
        status_code=429,
        headers={"Retry-After": str(max(int(remaining), 1))},
    )


def register_login_failure(request: Request) -> None:
    """Count a failed login attempt against the client's key."""
    cfg = RateLimitConfig.from_settings()
    if not cfg.enabled:
        return
    limiter.record_failure(get_client_key(request, cfg), cfg)


def register_login_success(request: Request) -> None:
    """Reset the client's counter after a successful login."""
    cfg = RateLimitConfig.from_settings()
    if not cfg.enabled:
        return
    limiter.record_success(get_client_key(request, cfg))
