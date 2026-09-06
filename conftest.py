"""Root pytest configuration for Docky.

Critical ordering constraint
----------------------------
Some Docky modules read the data directory **at import time**:

- ``orchestrator/app/agent_manager/client.py`` instantiates a global
  ``agent_manager = AgentManager()`` singleton at module import, and
  ``AgentManager.__init__`` calls ``get_data_dir()``.
- Every agent/orchestrator config helper reads ``DOCKY_DATA_DIR`` lazily at
  call time, so as long as the environment variable is set before the first
  ``agent.*`` / ``app.*`` import everything stays inside the test temp dir.

To make this guarantee robust, ``DOCKY_DATA_DIR`` is set *at conftest import
time* (before pytest collects/imports any test module that might import the
application packages), using a fresh temporary directory created right here.
"""

import os
import tempfile
from pathlib import Path

import bcrypt
import pytest
import yaml

# ---------------------------------------------------------------------------
# Test-only speed-up: lower the bcrypt cost factor
# ---------------------------------------------------------------------------
# bcrypt at cost 12 costs ~70-100 ms per checkpw/hashpw call. With hundreds
# of logins/rotations across the suite (rate_limit, csrf, password_rotation,
# auth setup), that single cost dominates the runtime (~39% of it).
#
# This file is ONLY ever loaded by pytest — application code in production
# never imports conftest — so patching ``bcrypt.gensalt`` here does not
# affect production hashing in any way: ``app.config``,
# ``app.auth.router``, ``app.routes.settings`` and
# ``app.auth.password_policy`` still call ``bcrypt.gensalt()`` with its
# default cost 12 when this wrapper is absent.
#
# The wrapper clamps any requested cost >= 12 down to 4 (bcrypt's minimum).
# The cost is embedded in the resulting hash string ($2b$04$...), so
# ``bcrypt.checkpw`` — including in unrelated assertions — keeps working:
# hashes generated during tests verify exactly as before, just faster.
_ORIG_GENSALT = bcrypt.gensalt
_TEST_BCRYPT_ROUNDS = 4  # minimum bcrypt cost; production default stays 12


def _test_gensalt(rounds: int = 12, prefix: bytes = b"2b") -> bytes:
    return _ORIG_GENSALT(_TEST_BCRYPT_ROUNDS, prefix)


bcrypt.gensalt = _test_gensalt  # type: ignore[assignment]

# Mot de passe utilisé pour les comptes de test: "docky123".
# Hash bcrypt GÉNÉRÉ dynamiquement UNE seule fois à l'import du conftest
# (coût 4 en tests via le wrapper ci-dessus, un seul appel par session
# pytest) — le vrai hash n'est plus codé en dur dans aucun fichier suivi
# du dépôt (voir docs/password-rotation.md, section « Nettoyage secrets »).
BCRYPT_DOCKY123 = bcrypt.hashpw(b"docky123", bcrypt.gensalt(12)).decode()

# Doit rester le premier module-level side effect: tout import de app.* /
# agent.* doit voir DOCKY_DATA_DIR déjà fixé.
_DATA_DIR = Path(tempfile.mkdtemp(prefix="docky-test-data-"))
os.environ.setdefault("DOCKY_DATA_DIR", str(_DATA_DIR))


@pytest.fixture(scope="session")
def data_dir() -> Path:
    """Return the shared, session-scoped test data directory.

    The directory is created once at conftest import time (see module
    docstring) so ``DOCKY_DATA_DIR`` can be set before any application
    import; this fixture merely hands out the same path.
    """
    return _DATA_DIR


# ---------------------------------------------------------------------------
# Helpers to seed configuration files in a data dir
# ---------------------------------------------------------------------------

def make_settings(
    data_dir: Path,
    jwt_secret: str = "test-jwt-secret",
    agents: list | None = None,
) -> Path:
    """Write a deterministic ``settings.yaml`` into *data_dir*.

    *jwt_secret* is fixed so JWT tokens are reproducible across tests; the
    ``agents`` list defaults to a single test agent.
    Returns the path to the written file.
    """
    settings = {
        "server": {"host": "0.0.0.0", "port": 8000},
        "llm": {"endpoint": "", "api_key": "", "model": ""},
        "firecrawl": {"endpoint": "", "api_key": ""},
        "security": {
            "jwt_secret": jwt_secret,
            "jwt_algorithm": "HS256",
            "jwt_expire_minutes": 1440,
        },
        "agents": agents
        if agents is not None
        else [
            {
                "name": "Test Agent",
                "url": "http://agent:8080",
                "api_key": "test-key",
            }
        ],
    }
    path = Path(data_dir) / "settings.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(settings, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    return path


def make_users(data_dir: Path, username: str = "admin", password: str | None = None) -> Path:
    """Write a deterministic ``users.yaml`` into *data_dir*.

    By default the account uses the pre-generated bcrypt hash of
    ``"docky123"`` so no password hashing happens at test time. Pass a
    *password* to seed a NON-default password instead (hashed at call time,
    ~70 ms) — needed by tests exercising the post-login flow, since a
    default-password account is now redirected to the forced rotation page
    (see ``docs/password-rotation.md``).
    Returns the path to the written file.
    """
    if password is None:
        password_hash = BCRYPT_DOCKY123
    else:
        password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(12)).decode()
    users = {
        "users": [
            {"username": username, "password_hash": password_hash},
        ],
    }
    path = Path(data_dir) / "users.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(users, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    return path


def make_api_keys(data_dir: Path) -> Path:
    """Write an empty deterministic ``api_keys.yaml`` into *data_dir*."""
    path = Path(data_dir) / "api_keys.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"api_keys": {}}, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    return path
