"""Validation pure et chemins de stacks (extrait de docker_manager).

Fonctions pures : aucune dépendance Docker / git / réseau. Ces fonctions sont
ré-exportées dans le namespace ``agent.docker_manager`` (façade).
"""

import re
from pathlib import Path

from agent.config import get_data_dir

_STACK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9.][A-Za-z0-9_.-]*$")


def get_stacks_dir() -> Path:
    """Return the path to the stacks directory inside the data dir."""
    return get_data_dir() / "stacks"


def validate_stack_name(name: str) -> str:
    """Return the stack name if valid, raise ValueError otherwise."""
    if not name or not _STACK_NAME_RE.match(name):
        raise ValueError(f"Invalid stack name: {name!r}")
    if ".." in name or "/" in name or "\\" in name:
        raise ValueError(f"Invalid stack name: {name!r}")
    return name


def validate_filename(filename: str) -> str:
    """Validate a filename within a stack directory."""
    if not filename:
        raise ValueError("Empty filename")
    if filename == "." or filename == "..":
        raise ValueError(f"Invalid filename: {filename!r}")
    if "/" in filename or "\\" in filename:
        raise ValueError(f"Filename must not contain path separators: {filename!r}")
    if ".." in filename:
        raise ValueError(f"Filename must not contain '..': {filename!r}")
    if not _SAFE_FILENAME_RE.match(filename):
        raise ValueError(f"Invalid filename: {filename!r}")
    return filename


def _stack_dir(name: str) -> Path:
    """Return the resolved path to a stack directory."""
    validate_stack_name(name)
    return (get_stacks_dir() / name).resolve()


def safe_join(stack_name: str, filename: str) -> Path:
    """Join *filename* to the stack directory and verify the resolved path
    stays inside the stack directory.

    Raises ``ValueError`` if the stack name or filename is invalid, or if a
    path traversal attempt is detected. Returns the resolved ``Path``.
    """
    validate_filename(filename)
    base = _stack_dir(stack_name)
    target = (base / filename).resolve()
    if base != target and base not in target.parents:
        raise ValueError("Path traversal detected")
    return target
