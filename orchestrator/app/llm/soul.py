"""Soul.md helpers (extraits de ``app.llm.client``).

``read_soul`` / ``update_soul`` sont utilisés par le system prompt builder
(``app.llm.prompt``) et par l'exécuteur d'outils (``app.llm.tools``). Tous les
symboles sont ré-exportés dans le namespace ``app.llm.client`` (façade).
"""

import logging
from pathlib import Path

from app.config import get_data_dir

logger = logging.getLogger(__name__)


def _soul_path() -> Path:
    return Path(get_data_dir()) / "soul.md"


def read_soul() -> str:
    """Read and return the content of ``soul.md``."""
    path = _soul_path()
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def update_soul(content: str) -> str:
    """Overwrite ``soul.md`` with *content* and return a confirmation message."""
    path = _soul_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return "soul.md updated successfully."
