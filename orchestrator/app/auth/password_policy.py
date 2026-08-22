"""Password rotation policy for Docky.

Handles the forced rotation of the default bootstrap password
(``docky123``, created by :func:`app.config.ensure_config_files`).

Two complementary detection mechanisms (see ``docs/password-rotation.md``):

1. **Explicit flag** — the bootstrap writes ``must_change_password: true``
   on the default admin account; the flag is cleared once the password is
   changed.
2. **Bcrypt safety net** — deployments created before the flag existed have
   no marker, so the password submitted at login is also checked against
   the known default value.

Cost note
---------
bcrypt (cost 12) costs ~70 ms per ``checkpw`` call. The hash of the default
password is therefore computed **once** at module import
(:data:`DEFAULT_PASSWORD_HASH`) and reused for every check — never
re-generated per request. The safety-net check only runs after a *successful*
authentication, i.e. at most once per login.
"""

import bcrypt

#: The well-known bootstrap password created by ``ensure_config_files()``.
DEFAULT_PASSWORD = "docky123"

#: Minimum accepted length for a new password — same rule as the existing
#: ``PUT /api/settings/password`` endpoint (see ``app.routes.settings``).
MIN_PASSWORD_LENGTH = 6

# Precomputed ONCE at import time (~70 ms, cost 12): bcrypt hash of the
# default password with a random salt. Every ``is_default_password`` call
# then costs a single constant-time ``checkpw`` against this constant.
DEFAULT_PASSWORD_HASH = bcrypt.hashpw(
    DEFAULT_PASSWORD.encode("utf-8"), bcrypt.gensalt()
)


def is_default_password(password: str) -> bool:
    """Return ``True`` if *password* is the well-known default password.

    Uses the module-level precomputed hash (see module docstring for the
    cost discussion). Malformed inputs simply return ``False``.
    """
    if not isinstance(password, str) or not password:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), DEFAULT_PASSWORD_HASH)
    except (ValueError, TypeError):
        return False


def rotation_required(user: dict, submitted_password: str) -> bool:
    """Decide whether *user* must go through the forced password change.

    ``True`` when either:

    - the account carries ``must_change_password: true`` (bootstrap flag), or
    - the submitted password IS the default one (safety net for accounts
      created before the flag existed).

    The flag cannot opt out of the safety net on purpose: an account whose
    password is still the public default must always rotate, whatever the
    stored flag says.
    """
    if not isinstance(user, dict):
        return False
    if bool(user.get("must_change_password", False)):
        return True
    return is_default_password(submitted_password)
