"""Agent configuration.

Reads the data directory from the ``DOCKY_DATA_DIR`` environment variable,
defaulting to ``/data``.
"""

import os
from pathlib import Path


def get_data_dir() -> Path:
    """Return the path to the data directory.

    Honours the ``DOCKY_DATA_DIR`` environment variable, falling back to
    ``/data``.
    """
    env_dir = os.environ.get("DOCKY_DATA_DIR")
    if env_dir:
        return Path(env_dir)
    default = Path("/data")
    if default.exists():
        return default
    # Development fallback: ./data relative to this package's parent
    return Path(__file__).resolve().parent.parent / "data"