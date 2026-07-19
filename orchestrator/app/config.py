"""Docky configuration loader.

All configuration files live under the data directory, which defaults to
``/data`` but can be overridden via the ``DOCKY_DATA_DIR`` environment
variable.
"""

import os
from pathlib import Path
from typing import Any, Dict, List

import yaml


def get_data_dir() -> Path:
    """Return the path to the data directory.

    Honours the ``DOCKY_DATA_DIR`` environment variable, falling back to
    ``/data``. When running outside a container (e.g. during development)
    a relative ``./data`` directory is used if the default does not exist.
    """
    env_dir = os.environ.get("DOCKY_DATA_DIR")
    if env_dir:
        return Path(env_dir)

    default = Path("/data")
    if default.exists():
        return default

    # Development fallback: ./data relative to the project root
    base = Path(__file__).resolve().parent.parent
    return base / "data"


def get_base_dir() -> Path:
    """Return the project root directory (parent of the ``app`` package)."""
    return Path(__file__).resolve().parent.parent


def _load_yaml(path: Path) -> Dict[str, Any]:
    """Load a YAML file and return its contents as a dict."""
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or {}


def load_settings() -> Dict[str, Any]:
    """Load ``settings.yaml`` from the data directory."""
    return _load_yaml(get_data_dir() / "settings.yaml")


def save_settings(settings: Dict[str, Any]):
    """Save ``settings.yaml`` to the data directory.

    The file is written in block style, preserving key order.
    """
    settings_path = get_data_dir() / "settings.yaml"
    with open(settings_path, "w", encoding="utf-8") as f:
        yaml.dump(settings, f, default_flow_style=False, sort_keys=False)


def load_users() -> Dict[str, Any]:
    """Load ``users.yaml`` from the data directory."""
    return _load_yaml(get_data_dir() / "users.yaml")


def save_users(users_data: Dict[str, Any]):
    """Save ``users.yaml`` to the data directory.

    The file is written in block style, preserving key order.
    """
    users_path = get_data_dir() / "users.yaml"
    with open(users_path, "w", encoding="utf-8") as f:
        yaml.dump(users_data, f, default_flow_style=False, sort_keys=False)


def load_api_keys() -> Dict[str, Any]:
    """Load ``api_keys.yaml`` from the data directory."""
    return _load_yaml(get_data_dir() / "api_keys.yaml")


def get_setting(key_path: str, default: Any = None) -> Any:
    """Retrieve a nested setting via dot-notation (e.g. ``security.jwt_secret``).

    Returns ``default`` if any part of the path is missing.
    """
    settings = load_settings()
    keys = key_path.split(".")
    current: Any = settings
    for key in keys:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return default
    return current


def find_user(username: str) -> Dict[str, Any] | None:
    """Look up a user by username from ``users.yaml``."""
    users_data = load_users()
    for user in users_data.get("users", []):
        if user.get("username") == username:
            return user
    return None


def ensure_config_files():
    """Crée les fichiers de config par défaut s'ils n'existent pas."""
    import os
    from pathlib import Path

    data_dir = Path(get_data_dir())
    data_dir.mkdir(parents=True, exist_ok=True)

    # settings.yaml
    settings_path = data_dir / "settings.yaml"
    if not settings_path.exists():
        default_settings = {
            "server": {"host": "0.0.0.0", "port": 8000},
            "llm": {"endpoint": "", "api_key": "", "model": ""},
            "firecrawl": {"api_key": ""},
            "security": {
                "jwt_secret": os.urandom(32).hex(),
                "jwt_algorithm": "HS256",
                "jwt_expire_minutes": 1440,
            },
            "agents": [],
        }
        with open(settings_path, "w", encoding="utf-8") as f:
            yaml.dump(default_settings, f, default_flow_style=False, sort_keys=False)

    # users.yaml — créer avec un user admin par défaut
    users_path = data_dir / "users.yaml"
    if not users_path.exists():
        import bcrypt

        default_hash = bcrypt.hashpw(b"docky123", bcrypt.gensalt()).decode()
        default_users = {
            "users": [
                {"username": "admin", "password_hash": default_hash},
            ],
        }
        with open(users_path, "w", encoding="utf-8") as f:
            yaml.dump(default_users, f, default_flow_style=False, sort_keys=False)

    # api_keys.yaml
    api_keys_path = data_dir / "api_keys.yaml"
    if not api_keys_path.exists():
        with open(api_keys_path, "w", encoding="utf-8") as f:
            yaml.dump({"api_keys": {}}, f, default_flow_style=False, sort_keys=False)

    # soul.md
    soul_path = data_dir / "soul.md"
    if not soul_path.exists():
        soul_path.write_text(
            "# Docky - SOUL\n\n"
            "Instructions et préférences accumulées au fil du temps.\n"
            "Ce fichier est la mémoire persistante du LLM.\n",
            encoding="utf-8",
        )

    # compose_reference.md — copier depuis l'app si pas dans /data/
    ref_path = data_dir / "compose_reference.md"
    if not ref_path.exists():
        bundled = Path(__file__).parent / "compose_reference.md"
        if bundled.exists():
            ref_path.write_text(bundled.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            ref_path.write_text(
                "# Docker Compose Reference\n\n"
                "Le champ version: est DEPRÉCIÉ. Ne PAS l'inclure.\n",
                encoding="utf-8",
            )