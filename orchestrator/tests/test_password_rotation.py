"""Tests de la rotation forcée du mot de passe par défaut (docky123).

Couvre le flux décrit dans ``docs/password-rotation.md`` :

- détection par flag ``must_change_password`` ET filet de sécurité bcrypt ;
- login d'un compte par défaut → redirection ``/change-password`` SANS
  émission du JWT de session (seul le cookie restreint ``docky_pwreset``
  est posé) ;
- confinement du token restreint : rejeté par ``verify_token``, donc par
  ``_check_auth``, le dashboard et toutes les pages protégées ;
- ``POST /change-password`` : validations, persistance, émission de la
  session normale ;
- flux inchangé pour un compte au mot de passe non-défaut.
"""

import bcrypt
import yaml

from app.auth.jwt_utils import (
    PASSWORD_CHANGE_EXPIRE_MINUTES,
    create_access_token,
    create_password_change_token,
    verify_password_change_token,
    verify_token,
)
from app.auth.password_policy import (
    DEFAULT_PASSWORD,
    DEFAULT_PASSWORD_HASH,
    is_default_password,
    rotation_required,
)
from orchestrator.tests._helpers import BCRYPT_DOCKY123, make_settings, make_users


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NEW_PASSWORD = "Nouveau-Pass-Securise!"

# Hash bcrypt (coût 12) d'un mot de passe NON-défaut, calculé une fois à
# l'import (~70 ms) pour éviter de re-hasher dans chaque test.
NON_DEFAULT_HASH = bcrypt.hashpw(b"Adm1n-Custom-Pass!", bcrypt.gensalt(12)).decode()
NON_DEFAULT_PASSWORD = "Adm1n-Custom-Pass!"


def _read_admin(data_dir) -> dict:
    """Return the ``admin`` entry from the current users.yaml."""
    users = yaml.safe_load((data_dir / "users.yaml").read_text(encoding="utf-8"))
    return next(u for u in users["users"] if u["username"] == "admin")


def _write_single_user(data_dir, **extra):
    """Overwrite users.yaml with one admin account carrying *extra* fields."""
    user = {"username": "admin", "password_hash": BCRYPT_DOCKY123}
    user.update(extra)
    (data_dir / "users.yaml").write_text(
        yaml.safe_dump({"users": [user]}, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def _login(client, username="admin", password=DEFAULT_PASSWORD):
    return client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )


# ---------------------------------------------------------------------------
# password_policy — détection
# ---------------------------------------------------------------------------


class TestPasswordPolicy:
    def test_is_default_password_detects_docky123(self):
        assert is_default_password("docky123") is True

    def test_is_default_password_rejects_other_values(self):
        assert is_default_password("wrong") is False
        assert is_default_password("") is False
        assert is_default_password(None) is False
        assert is_default_password(NEW_PASSWORD) is False

    def test_precomputed_hash_matches_default(self):
        """Garde-fou : la constante module-level hash bien 'docky123'."""
        assert bcrypt.checkpw(DEFAULT_PASSWORD.encode(), DEFAULT_PASSWORD_HASH)

    def test_rotation_required_via_flag_only(self):
        user = {"username": "admin", "must_change_password": True}
        assert rotation_required(user, NON_DEFAULT_PASSWORD) is True

    def test_rotation_required_via_safety_net_without_flag(self):
        # Compte créé avant l'introduction du flag : aucun marqueur, mais le
        # mot de passe soumis EST le mot de passe par défaut.
        user = {"username": "admin"}
        assert rotation_required(user, DEFAULT_PASSWORD) is True

    def test_flag_false_cannot_disable_safety_net(self):
        user = {"username": "admin", "must_change_password": False}
        assert rotation_required(user, DEFAULT_PASSWORD) is True

    def test_rotation_not_required_for_normal_account(self):
        user = {"username": "admin", "must_change_password": False}
        assert rotation_required(user, NON_DEFAULT_PASSWORD) is False


# ---------------------------------------------------------------------------
# jwt_utils — séparation des portées de tokens
# ---------------------------------------------------------------------------


class TestTokenScopes:
    def test_restricted_token_carries_purpose_claim(self, data_dir):
        make_settings(data_dir)
        from jose import jwt as _jwt

        from app.config import get_setting

        payload = _jwt.decode(
            create_password_change_token("admin"),
            get_setting("security.jwt_secret"),
            algorithms=["HS256"],
        )
        assert payload["sub"] == "admin"
        assert payload["purpose"] == "password_change"

    def test_restricted_token_is_short_lived(self, data_dir):
        make_settings(data_dir)
        from datetime import datetime, timezone

        from jose import jwt as _jwt

        from app.config import get_setting

        payload = _jwt.decode(
            create_password_change_token("admin"),
            get_setting("security.jwt_secret"),
            algorithms=["HS256"],
        )
        now = datetime.now(timezone.utc).timestamp()
        remaining = payload["exp"] - now
        assert 0 < remaining <= PASSWORD_CHANGE_EXPIRE_MINUTES * 60

    def test_verify_token_rejects_restricted_token(self, data_dir):
        make_settings(data_dir)
        assert verify_token(create_password_change_token("admin")) is None

    def test_verify_password_change_token_accepts_restricted(self, data_dir):
        make_settings(data_dir)
        assert verify_password_change_token(create_password_change_token("bob")) == "bob"

    def test_verify_password_change_token_rejects_session_token(self, data_dir):
        make_settings(data_dir)
        assert verify_password_change_token(create_access_token("admin")) is None

    def test_verify_password_change_token_rejects_garbage(self, data_dir):
        make_settings(data_dir)
        assert verify_password_change_token("") is None
        assert verify_password_change_token("garbage") is None

    def test_verify_password_change_token_rejects_wrong_purpose(self, data_dir):
        make_settings(data_dir)
        from datetime import datetime, timedelta, timezone

        from jose import jwt as _jwt

        from app.config import get_setting

        token = _jwt.encode(
            {
                "sub": "admin",
                "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
                "purpose": "something_else",
            },
            get_setting("security.jwt_secret"),
            algorithm="HS256",
        )
        assert verify_password_change_token(token) is None


# ---------------------------------------------------------------------------
# ensure_config_files — flag au bootstrap
# ---------------------------------------------------------------------------


def test_bootstrap_writes_must_change_password_flag(tmp_path, monkeypatch):
    """La création du compte admin par défaut pose must_change_password: true."""
    from app.config import ensure_config_files

    monkeypatch.setenv("DOCKY_DATA_DIR", str(tmp_path))
    ensure_config_files()

    users = yaml.safe_load((tmp_path / "users.yaml").read_text(encoding="utf-8"))
    admin = next(u for u in users["users"] if u["username"] == "admin")
    assert admin["must_change_password"] is True
    assert bcrypt.checkpw(b"docky123", admin["password_hash"].encode("utf-8"))


# ---------------------------------------------------------------------------
# Login — déclenchement de la rotation
# ---------------------------------------------------------------------------


def test_default_login_redirects_to_change_password_without_session(orchestrator_client):
    """Compte par défaut (flag du bootstrap) : PAS de JWT complet."""
    client = orchestrator_client
    resp = _login(client)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/change-password"

    set_cookie = resp.headers.get("set-cookie", "")
    assert "docky_pwreset=" in set_cookie
    assert "httponly" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()
    # Le JWT de session n'est PAS émis.
    assert "docky_token=" not in set_cookie
    assert client.cookies.get("docky_token") is None
    assert client.cookies.get("docky_pwreset")


def test_safety_net_catches_default_password_without_flag(orchestrator_client, data_dir):
    """Filet de sécurité : hash docky123 sans aucun flag → rotation forcée."""
    client = orchestrator_client
    _write_single_user(data_dir)  # hash docky123, pas de champ flag
    resp = _login(client)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/change-password"
    assert client.cookies.get("docky_token") is None
    assert client.cookies.get("docky_pwreset")


def test_flag_forces_rotation_even_with_custom_password(orchestrator_client, data_dir):
    """Flag explicite : rotation exigée même si le mdp n'est pas le défaut."""
    client = orchestrator_client
    _write_single_user(
        data_dir,
        password_hash=NON_DEFAULT_HASH,
        must_change_password=True,
    )
    resp = _login(client, password=NON_DEFAULT_PASSWORD)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/change-password"
    assert client.cookies.get("docky_token") is None


def test_non_default_account_keeps_normal_flow(orchestrator_client, data_dir):
    """Compte normal (mdp non-défaut, pas de flag) : flux historique intact."""
    client = orchestrator_client
    _write_single_user(data_dir, password_hash=NON_DEFAULT_HASH)
    resp = _login(client, password=NON_DEFAULT_PASSWORD)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"

    set_cookie = resp.headers.get("set-cookie", "")
    assert "docky_token=" in set_cookie
    assert "docky_pwreset=" not in set_cookie
    assert client.cookies.get("docky_token")
    assert client.cookies.get("docky_pwreset") is None


# ---------------------------------------------------------------------------
# Confinement du token restreint
# ---------------------------------------------------------------------------


def test_restricted_token_rejected_by_api(orchestrator_client, data_dir):
    """Le cookie restreint ne donne accès à AUCUN endpoint /api/*."""
    client = orchestrator_client
    make_settings(data_dir)
    client.cookies.set("docky_pwreset", create_password_change_token("admin"))

    assert client.get("/api/agents").status_code == 401
    assert client.get("/api/version").status_code == 401
    assert (
        client.put(
            "/api/settings/password",
            json={"current_password": "x", "new_password": "yyyyyy"},
        ).status_code
        == 401
    )


def test_restricted_token_rejected_by_protected_pages(orchestrator_client, data_dir):
    """Le cookie restreint ne donne accès à aucune page protégée."""
    client = orchestrator_client
    make_settings(data_dir)
    client.cookies.set("docky_pwreset", create_password_change_token("admin"))

    for path in ("/dashboard", "/settings", "/popup/logs", "/",):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 303, path
        assert resp.headers["location"] == "/login", path


def test_change_password_page_requires_valid_restricted_cookie(orchestrator_client, data_dir):
    """Sans cookie restreint valide → redirection vers /login."""
    client = orchestrator_client
    make_settings(data_dir)

    # Aucun cookie
    resp = client.get("/change-password", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"

    # Cookie invalide
    client.cookies.set("docky_pwreset", "garbage-token")
    resp = client.get("/change-password", follow_redirects=False)
    assert resp.headers["location"] == "/login"

    # Un JWT de SESSION ne remplace pas le token restreint
    client.cookies.set("docky_pwreset", create_access_token("admin"))
    resp = client.get("/change-password", follow_redirects=False)
    assert resp.headers["location"] == "/login"


def test_change_password_page_renders_form(orchestrator_client):
    """Le login par défaut permet d'afficher le formulaire de rotation."""
    client = orchestrator_client
    assert _login(client).status_code == 303

    resp = client.get("/change-password")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Pour des raisons de sécurité" in resp.text
    assert 'name="new_password"' in resp.text
    assert 'name="confirm_password"' in resp.text
    assert 'action="/change-password"' in resp.text


# ---------------------------------------------------------------------------
# POST /change-password — validations et succès
# ---------------------------------------------------------------------------


def _post_change_password(client, new=NEW_PASSWORD, confirm=NEW_PASSWORD):
    return client.post(
        "/change-password",
        data={"new_password": new, "confirm_password": confirm},
        follow_redirects=False,
    )


def test_rotation_success_full_flow(orchestrator_client, data_dir):
    """Parcours complet : login → rotation → session normale sur /dashboard."""
    client = orchestrator_client
    assert _login(client).status_code == 303
    assert client.cookies.get("docky_pwreset")

    resp = _post_change_password(client)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"

    # Le cookie restreint est supprimé, la session normale émise.
    assert client.cookies.get("docky_pwreset") is None
    assert client.cookies.get("docky_token")

    # users.yaml mis à jour : nouveau hash + flag désactivé.
    admin = _read_admin(data_dir)
    assert admin["must_change_password"] is False
    assert bcrypt.checkpw(NEW_PASSWORD.encode(), admin["password_hash"].encode())

    # Le dashboard est accessible avec la nouvelle session.
    dash = client.get("/dashboard", follow_redirects=False)
    assert dash.status_code == 200


def test_old_default_password_rejected_after_rotation(orchestrator_client, data_dir):
    client = orchestrator_client
    assert _login(client).status_code == 303
    assert _post_change_password(client).status_code == 303

    resp = client.get(
        "/logout", follow_redirects=False
    )  # clear cookies
    assert resp.status_code == 303

    # L'ancien mot de passe par défaut ne fonctionne plus.
    resp = _login(client, password=DEFAULT_PASSWORD)
    assert resp.headers["location"] == "/login?error=1"

    # Le nouveau ouvre une session normale (plus de rotation).
    resp = _login(client, password=NEW_PASSWORD)
    assert resp.headers["location"] == "/dashboard"
    assert client.cookies.get("docky_token")


def test_rotation_error_too_short(orchestrator_client, data_dir):
    """Même règle que PUT /api/settings/password : longueur minimale 6."""
    client = orchestrator_client
    assert _login(client).status_code == 303

    resp = _post_change_password(client, new="abc", confirm="abc")
    assert resp.status_code == 200
    assert "trop court" in resp.text.lower()

    # Rien n'a changé côté users.yaml, toujours en attente de rotation.
    admin = _read_admin(data_dir)
    assert bcrypt.checkpw(DEFAULT_PASSWORD.encode(), admin["password_hash"].encode())
    assert client.cookies.get("docky_pwreset")
    assert client.cookies.get("docky_token") is None


def test_rotation_error_confirmation_mismatch(orchestrator_client, data_dir):
    client = orchestrator_client
    assert _login(client).status_code == 303

    resp = _post_change_password(client, confirm="Autre-Pass-Mot!")
    assert resp.status_code == 200
    assert "confirmation" in resp.text.lower()

    admin = _read_admin(data_dir)
    assert bcrypt.checkpw(DEFAULT_PASSWORD.encode(), admin["password_hash"].encode())


def test_rotation_error_same_as_current_password(orchestrator_client, data_dir):
    """Refuser un « nouveau » mot de passe identique à l'actuel (docky123)."""
    client = orchestrator_client
    assert _login(client).status_code == 303

    resp = _post_change_password(client, new=DEFAULT_PASSWORD, confirm=DEFAULT_PASSWORD)
    assert resp.status_code == 200
    # NB : le message passe par {{ error }} avec auto-échappement Jinja2
    # (l'apostrophe devient &#39;) → on n'assert que la portion sans ' .
    assert "différent de l" in resp.text

    admin = _read_admin(data_dir)
    assert bcrypt.checkpw(DEFAULT_PASSWORD.encode(), admin["password_hash"].encode())


def test_rotation_without_pending_token_redirects_to_login(orchestrator_client):
    """POST sans cookie restreint valide → /login, rien n'est modifié."""
    client = orchestrator_client
    resp = _post_change_password(client)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    assert client.cookies.get("docky_token") is None


# ---------------------------------------------------------------------------
# Rate limiting — toujours actif pendant une rotation en attente
# ---------------------------------------------------------------------------


def _enable_rate_limit(data_dir, max_attempts):
    path = data_dir / "settings.yaml"
    settings = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    settings.setdefault("security", {})["rate_limit"] = {
        "enabled": True,
        "max_attempts": max_attempts,
        "window_seconds": 300,
        "trust_proxy": False,
    }
    path.write_text(
        yaml.safe_dump(settings, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def test_login_rate_limit_still_enforced_with_pending_rotation(orchestrator_client, data_dir):
    """Un compte en attente de rotation reste soumis au limiteur de /login."""
    from app.auth.rate_limit import reset_rate_limiter

    reset_rate_limiter()
    try:
        client = orchestrator_client
        _enable_rate_limit(data_dir, max_attempts=2)

        assert _login(client, password="wrong").status_code == 303  # échec 1
        assert _login(client, password="wrong").status_code == 303  # échec 2

        # Les bons identifiants seraient acceptés (puis redirigés vers la
        # rotation) mais l'IP est bloquée AVANT toute vérification.
        resp = _login(client)
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers
        assert client.cookies.get("docky_pwreset") is None
    finally:
        reset_rate_limiter()
