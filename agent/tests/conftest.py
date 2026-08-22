"""Shared fixtures for the agent test-suite (PHASE 1).

Every fixture here is fully hermetic: no real Docker daemon, no git, no
network, no /projects/Docky/data access. The ``DOCKY_DATA_DIR`` environment
variable is already pointed at a session temp dir by the root ``conftest.py``
before any ``agent.*`` import.
"""

import docker
import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fake Docker objects (hermetic replacement for the docker-py SDK)
# ---------------------------------------------------------------------------

class FakeImage:
    """Minimal stand-in for a docker-py ``Image`` model."""

    def __init__(self, tags=None, repo_digests=None, image_id="sha256:fakeimage"):
        # ``None`` means "use the default tag"; an explicit empty list stays empty
        # so tests can exercise images without any tag.
        self.tags = list(tags) if tags is not None else ["nginx:latest"]
        self.id = image_id
        self.attrs = {"RepoDigests": list(repo_digests) if repo_digests is not None else []}


class FakeContainer:
    """Minimal stand-in for a docker-py ``Container`` model."""

    def __init__(self, container_id="abcdef123456", name="web", image=None, attrs=None):
        self.id = container_id
        self.short_id = container_id[:12]
        self.name = name
        self.status = "running"
        self.image = image if image is not None else FakeImage()
        self.attrs = attrs if attrs is not None else {
            "Config": {"Image": "nginx:latest"},
            "State": {},
            "Created": "",
        }
        self.ports = {}

    def reload(self):
        return None


class _FakeContainersManager:
    def __init__(self, containers):
        self._by_id = {c.id: c for c in containers}
        self._by_name = {c.name: c for c in containers}

    def list(self, all=True):
        return list(self._by_id.values())

    def get(self, container_id):
        c = self._by_id.get(container_id) or self._by_name.get(container_id)
        if c is None:
            raise docker.errors.NotFound(f"container {container_id} not found")
        return c

    def run(self, *args, **kwargs):
        return FakeContainer()


class _FakeImagesManager:
    def __init__(self, images):
        self._by_ref = {}
        for img in images:
            if img.tags:
                for tag in img.tags:
                    self._by_ref[tag] = img
            self._by_ref[img.id] = img

    def get(self, ref):
        img = self._by_ref.get(ref)
        if img is None:
            raise docker.errors.ImageNotFound(f"image {ref} not found")
        return img

    def pull(self, *args, **kwargs):
        return FakeImage()


class _FakeApi:
    """API surface used by update-check / distribution lookups."""

    def inspect_distribution(self, *args, **kwargs):
        return {}


class FakeDockerClient:
    """Minimal stand-in for a docker-py ``DockerClient``."""

    def __init__(self, containers=None, images=None):
        self.containers = _FakeContainersManager(list(containers or []))
        self.images = _FakeImagesManager(list(images or []))
        self.api = _FakeApi()

    def events(self, decode=True):
        return iter([])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def api_key_header(monkeypatch):
    """Set the agent API key and return the matching auth header."""
    monkeypatch.setenv("DOCKY_AGENT_API_KEY", "test-key")
    return {"Authorization": "Bearer test-key"}


@pytest.fixture
def agent_client():
    """Return a ``TestClient`` bound to the agent FastAPI app.

    Imports ``agent.main`` lazily so the root conftest has already fixed
    ``DOCKY_DATA_DIR`` before the application package is loaded.
    """
    from agent.main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture
def mock_docker_client(monkeypatch):
    """Patch ``agent.docker_manager.get_docker_client`` with a fake client.

    The returned fake client is the one handed out by ``get_docker_client``,
    so tests can pre-populate containers/images before calling the code under
    test.
    """
    from agent import docker_manager

    fake = FakeDockerClient()
    monkeypatch.setattr(docker_manager, "get_docker_client", lambda: fake)
    return fake


@pytest.fixture
def mock_subprocess(monkeypatch):
    """Patch ``agent.docker_manager.subprocess.run`` with a controllable mock.

    Returns the ``unittest.mock.MagicMock`` so tests can set ``return_value`` /
    ``side_effect`` to simulate `docker manifest inspect`, `git`, ``ss``, etc.
    """
    from unittest import mock

    from agent import docker_manager

    run_mock = mock.MagicMock()
    monkeypatch.setattr(docker_manager.subprocess, "run", run_mock)
    return run_mock
