"""Tests for the container-edit git backup behaviour (Part B).

Verify that editing a container that belongs to a **managed** stack creates a
git backup of the stack (via ``_git_save``), while a **standalone** container
(no compose/stack managed by Docky) triggers no git backup and does not crash.

Hermetic: the data dir is redirected to ``tmp_path``, the git helpers are
stubbed (no real git repository), and ``compose_up`` / ``_recreate_container``
are faked so no Docker daemon is touched.
"""

from pathlib import Path

import pytest

from agent import docker_manager as dm
from agent.tests.conftest import FakeContainer, FakeDockerClient

COMPOSE = """\
services:
  web:
    image: nginx:latest
    ports:
      - "8080:80"
"""


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    """Redirect DOCKY_DATA_DIR to a per-test tmp dir and stub git helpers."""
    monkeypatch.setenv("DOCKY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(dm, "_git_init", lambda: None)
    monkeypatch.setattr(dm, "_git_save", lambda *args, **kwargs: None)


def _managed_stack(tmp_path: Path, name: str = "myapp") -> Path:
    stack_dir = Path(tmp_path) / "stacks" / name
    stack_dir.mkdir(parents=True, exist_ok=True)
    (stack_dir / "docker-compose.yml").write_text(COMPOSE, encoding="utf-8")
    return stack_dir


def _stack_container(container_id: str = "abc1"):
    """A container belonging to the managed ``myapp`` compose service ``web``."""
    attrs = {
        "Config": {
            "Image": "nginx:latest",
            "Labels": {
                "com.docker.compose.project": "myapp",
                "com.docker.compose.service": "web",
            },
        }
    }
    return FakeContainer(container_id=container_id, name="web", attrs=attrs)


def _standalone_container():
    """A container with no compose project (standalone)."""
    attrs = {"Config": {"Image": "nginx:latest", "Labels": {}}}
    return FakeContainer(container_id="solo1", name="web", attrs=attrs)


# ---------------------------------------------------------------------------
# Stack gérée → le backup git de la stack est créé
# ---------------------------------------------------------------------------

async def test_managed_stack_edit_creates_git_backup(tmp_path, monkeypatch):
    stack_dir = _managed_stack(tmp_path)

    fake = FakeDockerClient(containers=[_stack_container()])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)

    git_calls = []
    monkeypatch.setattr(dm, "_git_save", lambda stack, msg=None: git_calls.append((stack, msg)))

    async def no_up(stack):
        return {"success": True}
    monkeypatch.setattr(dm, "compose_up", no_up)

    spec = {
        "name": "web",
        "image": "nginx:1.25",
        "restart_policy": "no",
        "ports": [],
        "volumes": [],
        "env": [],
    }

    result = await dm.update_container("abc1", spec)

    assert result["success"] is True
    # La nouvelle config a bien été écrite dans le compose.
    written = (stack_dir / "docker-compose.yml").read_text(encoding="utf-8")
    assert "nginx:1.25" in written
    # Et un backup git de la stack concernée a bien été créé.
    assert git_calls and git_calls[0][0] == "myapp"


async def test_managed_stack_edit_no_change_is_harmless(tmp_path, monkeypatch):
    """Édition sans changement de config : pas de crash, backup quand même."""
    _managed_stack(tmp_path)

    fake = FakeDockerClient(containers=[_stack_container()])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)

    git_calls = []
    monkeypatch.setattr(dm, "_git_save", lambda stack, msg=None: git_calls.append((stack, msg)))
    monkeypatch.setattr(dm, "compose_up", lambda stack: __import__("asyncio").sleep(0) or {"success": True})

    spec = {
        "name": "web",
        "image": "nginx:latest",
        "restart_policy": "no",
        "ports": [],
        "volumes": [],
        "env": [],
    }

    result = await dm.update_container("abc1", spec)
    assert result["success"] is True
    assert git_calls and git_calls[0][0] == "myapp"


# ---------------------------------------------------------------------------
# Standalone → aucun backup git, aucun crash
# ---------------------------------------------------------------------------

async def test_standalone_edit_no_git_no_crash(tmp_path, monkeypatch):
    fake = FakeDockerClient(containers=[_standalone_container()])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)

    git_calls = []
    monkeypatch.setattr(dm, "_git_save", lambda stack, msg=None: git_calls.append((stack, msg)))

    recreated = {}
    async def fake_recreate(c, container_id, spec, client, attrs):
        recreated["called"] = True
        return {"success": True, "output": "recreated"}
    monkeypatch.setattr(dm, "_recreate_container", fake_recreate)

    result = await dm.update_container("solo1", {"name": "web", "image": "nginx:latest"})

    assert result["success"] is True
    assert recreated.get("called") is True
    # Aucun backup git pour un container standalone (pas de stack gérée).
    assert git_calls == []
