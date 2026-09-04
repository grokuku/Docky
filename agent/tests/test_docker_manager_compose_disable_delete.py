"""Tests for the Compose-managed behaviour of ``disable_container`` /
``delete_container``.

When the container belongs to a **Docky-managed** Compose stack (directory
under ``/data/stacks/``), the action must also edit the compose file:

- ``disable_container`` → sets ``restart: "no"`` on the service + git backup,
  then ``docker update --restart=no`` + ``docker stop``.
- ``delete_container`` → removes the service from ``services:`` + git backup,
  then ``docker rm -f``.

External / standalone containers keep the legacy docker-level behaviour (no
file modification).  Edge cases: service not found in compose (container action
still runs, no crash, file untouched), malformed compose (no crash), last
service removed (compose left empty).

Hermetic: Docker SDK replaced by fakes, ``DOCKY_DATA_DIR`` redirected to a
per-test tmp dir, git helpers (``_git_init``/``_git_save``) stubbed.
"""

from pathlib import Path

import pytest

from agent import docker_manager as dm
from agent.tests.conftest import FakeContainer, FakeDockerClient

COMPOSE = """\
# @name myapp
# @category web
services:
  web:
    image: nginx:latest
    ports:
      - "8080:80"
  db:
    image: postgres:14
"""


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(dm, "_git_init", lambda: None)
    monkeypatch.setattr(dm, "_git_save", lambda *args, **kwargs: None)


def _managed_stack(tmp_path: Path, name: str = "myapp", content: str = COMPOSE) -> Path:
    stack_dir = Path(tmp_path) / "stacks" / name
    stack_dir.mkdir(parents=True, exist_ok=True)
    (stack_dir / "docker-compose.yml").write_text(content, encoding="utf-8")
    return stack_dir


def _stack_container(container_id: str = "abc1", project: str = "myapp", service: str = "web"):
    attrs = {
        "Config": {
            "Image": "nginx:latest",
            "Labels": {
                "com.docker.compose.project": project,
                "com.docker.compose.service": service,
            },
        }
    }
    return FakeContainer(container_id=container_id, name=service, attrs=attrs)


def _standalone_container():
    attrs = {"Config": {"Image": "nginx:latest", "Labels": {}}}
    return FakeContainer(container_id="solo1", name="web", attrs=attrs)


def _external_compose_container():
    """A Compose container whose project is NOT managed by Docky."""
    return _stack_container(container_id="ext1", project="external-stack", service="web")


# ---------------------------------------------------------------------------
# disable_container — managed compose → compose restart:no + git + update/stop
# ---------------------------------------------------------------------------

def test_disable_managed_compose_sets_restart_no_and_git(tmp_path, monkeypatch):
    stack_dir = _managed_stack(tmp_path)
    fake = FakeDockerClient(containers=[_stack_container()])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)

    git_calls = []
    monkeypatch.setattr(dm, "_git_save", lambda stack, msg=None: git_calls.append((stack, msg)))

    assert dm.disable_container("abc1") is True

    written = (stack_dir / "docker-compose.yml").read_text(encoding="utf-8")
    assert 'restart: "no"' in written
    # comments preserved by the textual edit
    assert "# @name myapp" in written
    assert "image: nginx:latest" in written
    # git backup of the managed stack
    assert git_calls and git_calls[0][0] == "myapp"
    # docker update + stop still applied
    c = fake.containers.get("abc1")
    assert getattr(c, "_update_calls", []) == [{"restart_policy": {"Name": "no"}}]
    assert getattr(c, "_stop_calls", 0) == 1


def test_disable_managed_compose_replaces_existing_restart(tmp_path, monkeypatch):
    content = """\
services:
  web:
    image: nginx
    restart: always
  db:
    image: postgres
"""
    stack_dir = _managed_stack(tmp_path, content=content)
    fake = FakeDockerClient(containers=[_stack_container()])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)
    monkeypatch.setattr(dm, "_git_save", lambda *a, **k: None)

    assert dm.disable_container("abc1") is True
    written = (stack_dir / "docker-compose.yml").read_text(encoding="utf-8")
    assert 'restart: "no"' in written
    assert "restart: always" not in written
    assert "image: nginx" in written
    import yaml
    assert yaml.safe_load(written)["services"]["web"]["restart"] == "no"


# ---------------------------------------------------------------------------
# delete_container — managed compose → service removed + git + rm
# ---------------------------------------------------------------------------

def test_delete_managed_compose_removes_service_and_git(tmp_path, monkeypatch):
    stack_dir = _managed_stack(tmp_path)
    fake = FakeDockerClient(containers=[_stack_container()])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)

    git_calls = []
    monkeypatch.setattr(dm, "_git_save", lambda stack, msg=None: git_calls.append((stack, msg)))

    assert dm.delete_container("abc1") is True

    written = (stack_dir / "docker-compose.yml").read_text(encoding="utf-8")
    import yaml
    services = yaml.safe_load(written)["services"]
    assert "web" not in services
    assert "db" in services
    assert "# @name myapp" in written
    assert git_calls and git_calls[0][0] == "myapp"
    c = fake.containers.get("abc1")
    assert getattr(c, "_remove_calls", []) == [{"force": True}]


def test_delete_managed_compose_last_service_leaves_empty(tmp_path, monkeypatch):
    content = """\
services:
  web:
    image: busybox
"""
    stack_dir = _managed_stack(tmp_path, content=content)
    fake = FakeDockerClient(containers=[_stack_container()])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)
    monkeypatch.setattr(dm, "_git_save", lambda *a, **k: None)

    assert dm.delete_container("abc1") is True
    written = (stack_dir / "docker-compose.yml").read_text(encoding="utf-8")
    import yaml
    assert yaml.safe_load(written).get("services") in (None, {})
    assert "web" not in written


# ---------------------------------------------------------------------------
# Service not found in compose → container action still runs + warning, file
# ---------------------------------------------------------------------------

def test_disable_service_not_in_compose_still_disables(tmp_path, monkeypatch, caplog):
    stack_dir = _managed_stack(tmp_path)  # compose defines the default stack
    # container belongs to the managed project but a service absent from file
    container = _stack_container(container_id="abc1", service="ghost")
    fake = FakeDockerClient(containers=[container])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)
    monkeypatch.setattr(dm, "_git_save", lambda *a, **k: None)

    before = (stack_dir / "docker-compose.yml").read_text(encoding="utf-8")

    with caplog.at_level("WARNING", logger="agent.docker_manager"):
        assert dm.disable_container("abc1") is True

    assert (stack_dir / "docker-compose.yml").read_text(encoding="utf-8") == before
    c = fake.containers.get("abc1")
    assert getattr(c, "_update_calls", []) == [{"restart_policy": {"Name": "no"}}]
    assert getattr(c, "_stop_calls", 0) == 1


def test_delete_service_not_in_compose_still_removes(tmp_path, monkeypatch, caplog):
    stack_dir = _managed_stack(tmp_path)
    container = _stack_container(container_id="abc1", service="ghost")
    fake = FakeDockerClient(containers=[container])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)
    monkeypatch.setattr(dm, "_git_save", lambda *a, **k: None)

    before = (stack_dir / "docker-compose.yml").read_text(encoding="utf-8")

    with caplog.at_level("WARNING", logger="agent.docker_manager"):
        assert dm.delete_container("abc1") is True

    assert (stack_dir / "docker-compose.yml").read_text(encoding="utf-8") == before
    assert getattr(fake.containers.get("abc1"), "_remove_calls", []) == [{"force": True}]


# ---------------------------------------------------------------------------
# Malformed / invalid compose → no crash, no corruption, container action only
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action", ["disable_container", "delete_container"])
def test_invalid_compose_not_corrupted_and_no_crash(tmp_path, monkeypatch, action):
    bad = "{not: [valid yaml"
    stack_dir = _managed_stack(tmp_path, content=bad)
    fake = FakeDockerClient(containers=[_stack_container()])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)
    monkeypatch.setattr(dm, "_git_save", lambda *a, **k: None)

    result = getattr(dm, action)("abc1")
    assert result is True
    assert (stack_dir / "docker-compose.yml").read_text(encoding="utf-8") == bad


# ---------------------------------------------------------------------------
# External / standalone → legacy behaviour, no compose file touched
# ---------------------------------------------------------------------------

def test_disable_standalone_no_compose(tmp_path, monkeypatch):
    fake = FakeDockerClient(containers=[_standalone_container()])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)
    git_calls = []
    monkeypatch.setattr(dm, "_git_save", lambda *a, **k: git_calls.append(a))

    assert dm.disable_container("solo1") is True
    c = fake.containers.get("solo1")
    assert getattr(c, "_update_calls", []) == [{"restart_policy": {"Name": "no"}}]
    assert getattr(c, "_stop_calls", 0) == 1
    assert git_calls == []


def test_disable_external_compose_no_file_modification(tmp_path, monkeypatch):
    """External Compose project (not managed by Docky) → no file edit, no git."""
    fake = FakeDockerClient(containers=[_external_compose_container()])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)
    git_calls = []
    monkeypatch.setattr(dm, "_git_save", lambda *a, **k: git_calls.append(a))

    assert dm.disable_container("ext1") is True
    c = fake.containers.get("ext1")
    assert getattr(c, "_update_calls", []) == [{"restart_policy": {"Name": "no"}}]
    assert git_calls == []


def test_delete_standalone_no_compose(tmp_path, monkeypatch):
    fake = FakeDockerClient(containers=[_standalone_container()])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)
    git_calls = []
    monkeypatch.setattr(dm, "_git_save", lambda *a, **k: git_calls.append(a))

    assert dm.delete_container("solo1") is True
    assert getattr(fake.containers.get("solo1"), "_remove_calls", []) == [{"force": True}]
    assert git_calls == []


def test_delete_external_compose_no_file_modification(tmp_path, monkeypatch):
    fake = FakeDockerClient(containers=[_external_compose_container()])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)
    git_calls = []
    monkeypatch.setattr(dm, "_git_save", lambda *a, **k: git_calls.append(a))

    assert dm.delete_container("ext1") is True
    assert getattr(fake.containers.get("ext1"), "_remove_calls", []) == [{"force": True}]
    assert git_calls == []
