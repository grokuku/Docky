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


def load_users() -> Dict[str, Any]:
    """Load ``users.yaml`` from the data directory."""
    return _load_yaml(get_data_dir() / "users.yaml")


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