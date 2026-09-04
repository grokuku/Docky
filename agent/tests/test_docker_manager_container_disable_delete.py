"""Tests for ``disable_container`` and ``delete_container``.

Both actions must work on **Compose** containers (identified by the
``com.docker.compose.*`` labels) and on **standalone** containers, without
modifying the Compose file.

- ``disable_container`` → ``docker update --restart=no`` (SDK ``update``) then
  ``docker stop`` (SDK ``stop``).
- ``delete_container`` → ``docker rm -f`` (SDK ``remove(force=True)``).

Hermetic: the Docker SDK is replaced by ``FakeDockerClient`` / ``FakeContainer``
(no real daemon).
"""

import pytest

from agent import docker_manager as dm
from agent.tests.conftest import FakeContainer, FakeDockerClient


def _compose_container(container_id="abc1"):
    """A container belonging to a Compose project (via labels)."""
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


def _standalone_container(container_id="solo1"):
    """A container with no Compose project (standalone)."""
    attrs = {"Config": {"Image": "nginx:latest", "Labels": {}}}
    return FakeContainer(container_id=container_id, name="web", attrs=attrs)


# ---------------------------------------------------------------------------
# disable_container
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("container", [_compose_container(), _standalone_container()])
def test_disable_container_sets_restart_no_and_stops(monkeypatch, container):
    fake = FakeDockerClient(containers=[container])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)

    assert dm.disable_container(container.id) is True

    # docker update --restart=no
    assert getattr(container, "_update_calls", []) == [{"restart_policy": {"Name": "no"}}]
    # docker stop
    assert getattr(container, "_stop_calls", 0) == 1


def test_disable_container_missing_returns_false(monkeypatch):
    fake = FakeDockerClient(containers=[])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)

    assert dm.disable_container("missing") is False


# ---------------------------------------------------------------------------
# delete_container
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("container", [_compose_container(), _standalone_container()])
def test_delete_container_force_removes(monkeypatch, container):
    fake = FakeDockerClient(containers=[container])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)

    assert dm.delete_container(container.id) is True

    # docker rm -f
    assert getattr(container, "_remove_calls", []) == [{"force": True}]


def test_delete_container_missing_returns_false(monkeypatch):
    fake = FakeDockerClient(containers=[])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)

    assert dm.delete_container("missing") is False
