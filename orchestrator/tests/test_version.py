"""Tests de la résolution de version unifiée (``app.version``).

Chaîne validée : ``DOCKY_VERSION`` (env) > ``version.txt`` > ``"0.0.0"``.
Voir ``docs/versioning-unification.md``.
"""

from pathlib import Path

import pytest

import app.version as version_mod
from app.version import DEFAULT_VERSION, _find_version_path, get_version


# ---------------------------------------------------------------------------
# Résolution unitaire
# ---------------------------------------------------------------------------

def test_env_var_has_priority(monkeypatch):
    """DOCKY_VERSION définie → elle gagne, même si le fichier existe."""
    monkeypatch.setenv("DOCKY_VERSION", "9.9.9")
    assert get_version() == "9.9.9"


def test_env_var_strips_whitespace(monkeypatch):
    monkeypatch.setenv("DOCKY_VERSION", "  1.2.3  ")
    assert get_version() == "1.2.3"


def test_empty_env_var_falls_through_to_file(monkeypatch, tmp_path):
    """Une env vide/blanche est ignorée → lecture du fichier."""
    f = tmp_path / "version.txt"
    f.write_text("2.5.0\n", encoding="utf-8")
    monkeypatch.setenv("DOCKY_VERSION", "   ")
    assert get_version(path=f) == "2.5.0"


def test_file_is_read(tmp_path):
    f = tmp_path / "version.txt"
    f.write_text("3.14.15\n", encoding="utf-8")
    assert get_version(env_value=None, path=f) == "3.14.15"


def test_missing_file_falls_back_to_default(tmp_path):
    missing = tmp_path / "does-not-exist.txt"
    assert get_version(env_value=None, path=missing) == DEFAULT_VERSION
    assert DEFAULT_VERSION == "0.0.0"


@pytest.mark.parametrize("content", ["", "   \n\t "])
def test_blank_file_falls_back_to_default(tmp_path, content):
    """Fichier vide ou blanc → traité comme absent."""
    f = tmp_path / "version.txt"
    f.write_text(content, encoding="utf-8")
    assert get_version(env_value=None, path=f) == DEFAULT_VERSION


def test_unreadable_file_falls_back_to_default(tmp_path):
    """Un répertoire à la place du fichier ne fait pas crasher (OSError)."""
    directory = tmp_path / "version.txt"
    directory.mkdir()
    assert get_version(env_value=None, path=directory) == DEFAULT_VERSION


def test_find_version_path_prefers_existing_file():
    """En checkout source : retombe sur la racine du dépôt (fichier existant)."""
    path = _find_version_path()
    assert path.is_file()
    assert path.name == "version.txt"


# ---------------------------------------------------------------------------
# Smoke : cohérence bout-en-bout orchestrateur
# ---------------------------------------------------------------------------

def _repo_version() -> str:
    return Path(version_mod._VERSION_PATH).read_text(encoding="utf-8").strip()


def test_get_version_matches_repo_version_txt(monkeypatch):
    """Sans env, get_version() == contenu de version.txt du dépôt."""
    monkeypatch.delenv("DOCKY_VERSION", raising=False)
    assert get_version() == _repo_version()


def test_fastapi_app_exposes_repo_version(monkeypatch):
    """FastAPI(version=…) n'est plus codée en dur : elle vaut version.txt."""
    from app.main import app

    monkeypatch.delenv("DOCKY_VERSION", raising=False)
    assert app.version == _repo_version()


def test_package_dunder_version_matches(monkeypatch):
    """app.__version__ est aligné sur version.txt (plus de '0.1.0' en dur)."""
    import app

    monkeypatch.delenv("DOCKY_VERSION", raising=False)
    assert app.__version__ == _repo_version()


def test_api_version_endpoint_returns_resolved_version(auth_client, monkeypatch):
    """GET /api/version expose exactement version.txt du dépôt."""
    monkeypatch.delenv("DOCKY_VERSION", raising=False)
    resp = auth_client.get("/api/version")
    assert resp.status_code == 200
    assert resp.json()["version"] == _repo_version()


def test_api_version_endpoint_honors_env_override(auth_client, monkeypatch):
    """DOCKY_VERSION (lue à l'appel) prime aussi sur l'endpoint HTTP."""
    monkeypatch.setenv("DOCKY_VERSION", "8.8.8")
    resp = auth_client.get("/api/version")
    assert resp.status_code == 200
    assert resp.json()["version"] == "8.8.8"


def test_api_version_check_reports_same_orchestrator_version(
    auth_client, mock_agent_manager, monkeypatch
):
    """/api/version-check compare bien la version résolue (pas 'unknown')."""
    monkeypatch.delenv("DOCKY_VERSION", raising=False)
    resp = auth_client.get("/api/version-check")
    assert resp.status_code == 200
    body = resp.json()
    assert body["orchestrator_version"] == _repo_version()
