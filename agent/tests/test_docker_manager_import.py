"""Tests for ``agent.docker_manager.import_stack``.

Fully hermetic: the data dir is redirected to ``tmp_path`` per test and the
git helpers (``_git_init`` / ``_git_save``) are stubbed so no real git
repository is ever created or committed.
"""

from pathlib import Path

import pytest

from agent import docker_manager as dm

SIMPLE_COMPOSE = """\
services:
  app:
    image: nginx:latest
    ports:
      - "8080:80"
"""

VOLUMES_COMPOSE = """\
services:
  app:
    image: nginx:latest
    volumes:
      - ./data:/var/lib/data
      - /etc/localtime:/etc/localtime:ro
      - ${DATA_DIR}:/data
      - ../shared:/shared
"""


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    """Redirect DOCKY_DATA_DIR to a per-test tmp dir and stub git calls."""
    monkeypatch.setenv("DOCKY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(dm, "_git_init", lambda: None)
    monkeypatch.setattr(dm, "_git_save", lambda *args, **kwargs: None)


def _make_source(tmp_path: Path, name: str = "mysource", compose: str = SIMPLE_COMPOSE,
                 with_env: bool = True) -> Path:
    source = tmp_path / name
    source.mkdir(parents=True, exist_ok=True)
    (source / "docker-compose.yml").write_text(compose, encoding="utf-8")
    if with_env:
        (source / ".env").write_text("FOO=bar\n", encoding="utf-8")
    return source


def _stacks_dir() -> Path:
    return Path(dm.get_data_dir()) / "stacks"


# ---------------------------------------------------------------------------
# Nominal import
# ---------------------------------------------------------------------------

def test_import_stack_nominal(tmp_path):
    source = _make_source(tmp_path)

    result = dm.import_stack(str(source))

    assert result["success"] is True
    assert result["name"] == source.name
    assert "error" not in result

    target = _stacks_dir() / source.name
    assert target.exists()

    compose = (target / "docker-compose.yml").read_text(encoding="utf-8")
    assert "@name:" in compose
    assert source.name in compose

    env = (target / ".env").read_text(encoding="utf-8")
    assert env == "FOO=bar\n"


def test_import_stack_default_name_is_source_name(tmp_path):
    source = _make_source(tmp_path)
    result = dm.import_stack(str(source))
    assert result["success"] is True
    assert result["name"] == source.name


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

def test_import_stack_missing_source(tmp_path):
    missing = tmp_path / "does-not-exist"
    result = dm.import_stack(str(missing))
    assert result["success"] is False
    assert "does not exist" in result["error"]


def test_import_stack_no_compose_file(tmp_path):
    source = tmp_path / "empty"
    source.mkdir()
    result = dm.import_stack(str(source))
    assert result["success"] is False
    assert "No docker-compose.yml found" in result["error"]


def test_import_stack_fallback_compose_yaml(tmp_path):
    source = tmp_path / "yaml-source"
    source.mkdir()
    (source / "compose.yaml").write_text(SIMPLE_COMPOSE, encoding="utf-8")

    result = dm.import_stack(str(source))

    assert result["success"] is True
    target = _stacks_dir() / source.name
    assert (target / "docker-compose.yml").exists()
    assert "@name:" in (target / "docker-compose.yml").read_text(encoding="utf-8")


def test_import_stack_invalid_stack_name(tmp_path):
    source = _make_source(tmp_path)
    result = dm.import_stack(str(source), stack_name="Invalid Name")
    assert result["success"] is False
    assert "Invalid stack name" in result["error"]


def test_import_stack_existing_target(tmp_path):
    source = _make_source(tmp_path)
    first = dm.import_stack(str(source))
    assert first["success"] is True

    second = dm.import_stack(str(source))
    assert second["success"] is False
    assert "already exists" in second["error"]


# ---------------------------------------------------------------------------
# Volume path conversion
# ---------------------------------------------------------------------------

def test_import_stack_volume_conversions(tmp_path):
    source = _make_source(tmp_path, compose=VOLUMES_COMPOSE)

    result = dm.import_stack(str(source))

    assert result["success"] is True
    # ./data -> absolute path conversion recorded
    assert any(conv.startswith("./data → ") for conv in result["conversions"])
    # Variable + parent-directory volumes produce warnings, not errors
    assert any("${DATA_DIR}" in w for w in result["warnings"])
    assert any("../shared" in w for w in result["warnings"])

    target = _stacks_dir() / source.name
    written = (target / "docker-compose.yml").read_text(encoding="utf-8")
    # Relative volume resolved to an absolute path under the source dir
    assert f"- {source}/data:/var/lib/data" in written
    # Absolute path untouched
    assert "- /etc/localtime:/etc/localtime:ro" in written
    # Variable + parent paths left as-is
    assert "- ${DATA_DIR}:/data" in written
    assert "- ../shared:/shared" in written


def test_import_stack_volume_absolute_unchanged(tmp_path):
    compose = """\
services:
  app:
    image: nginx
    volumes:
      - /data/volume:/data
"""
    source = _make_source(tmp_path, compose=compose)

    result = dm.import_stack(str(source))

    assert result["success"] is True
    assert result["conversions"] == []
    written = (_stacks_dir() / source.name / "docker-compose.yml").read_text(encoding="utf-8")
    assert "- /data/volume:/data" in written


# ---------------------------------------------------------------------------
# dry_run mode
# ---------------------------------------------------------------------------

def test_import_stack_dry_run_writes_nothing(tmp_path):
    source = _make_source(tmp_path, compose=VOLUMES_COMPOSE)

    result = dm.import_stack(str(source), stack_name="drystack", dry_run=True)

    assert result["success"] is True
    assert result["name"] == "drystack"
    assert any(conv.startswith("./data → ") for conv in result["conversions"])
    assert result["warnings"]
    assert "preview" in result
    assert "@name: drystack" in result["preview"]
    assert f"{source}/data:/var/lib/data" in result["preview"]

    # Nothing may have been written on disk.
    assert not _stacks_dir().exists() or not (_stacks_dir() / "drystack").exists()
    assert not (source / "docker-compose.yml").read_text(encoding="utf-8").startswith("# ===")


def test_import_stack_dry_run_allows_existing_target(tmp_path):
    source = _make_source(tmp_path)
    # Real import first so the target exists.
    assert dm.import_stack(str(source))["success"] is True

    # dry_run still returns a preview instead of the "already exists" error.
    result = dm.import_stack(str(source), dry_run=True)
    assert result["success"] is True
    assert "preview" in result
