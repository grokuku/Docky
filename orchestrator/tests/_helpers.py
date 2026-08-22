"""Stable, importable re-exports of the root conftest config helpers.

The root ``conftest.py`` cannot be reached via ``import conftest``: pytest
imports every directory-level conftest under the same ``conftest`` basename,
so which one wins depends on sys.path ordering. Loading it from disk under a
unique module name makes ``make_settings`` / ``make_users`` / ``make_api_keys``
/ ``BCRYPT_DOCKY123`` importable as ``orchestrator.tests._helpers`` regardless
of the collection order (single dir or full suite).
"""

import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent  # repository root
_spec = importlib.util.spec_from_file_location("_root_conftest", _ROOT / "conftest.py")
_root_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_root_conftest)

BCRYPT_DOCKY123 = _root_conftest.BCRYPT_DOCKY123
make_settings = _root_conftest.make_settings
make_users = _root_conftest.make_users
make_api_keys = _root_conftest.make_api_keys
