"""Tests for the update-check helpers in ``agent.docker_manager``.

Everything is mocked: no real registry, no ``docker manifest inspect``, no
daemon distribution endpoint.
"""

import subprocess
import time
from unittest import mock

import pytest

from agent import docker_manager as dm
from agent.tests.conftest import FakeContainer, FakeDockerClient, FakeImage


# ---------------------------------------------------------------------------
# Shared helpers / fixtures
# ---------------------------------------------------------------------------

def _completed(stdout="", stderr="", returncode=0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def _clear_update_check_cache():
    """The in-memory TTL cache must be empty before/after every test."""
    dm._update_check_cache.clear()
    yield
    dm._update_check_cache.clear()


@pytest.fixture
def _patch_container_lookup(monkeypatch):
    """Return a namespace to build a fake client + mocked digest helpers."""
    state = {
        "remote": mock.Mock(),
        "local": mock.Mock(),
    }

    def _setup(container, local_digests, remote_info):
        fake = FakeDockerClient(containers=[container])
        monkeypatch.setattr(dm, "get_docker_client", lambda: fake)
        state["local"].side_effect = lambda image: local_digests
        state["remote"].side_effect = lambda repo, tag: remote_info
        monkeypatch.setattr(dm, "_local_repo_digests", state["local"])
        monkeypatch.setattr(dm, "_remote_manifest_check", state["remote"])
        return fake

    state["setup"] = _setup
    return state


# ---------------------------------------------------------------------------
# check_image_update
# ---------------------------------------------------------------------------

def test_check_image_update_container_not_found(monkeypatch):
    fake = FakeDockerClient(containers=[])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)

    result = dm.check_image_update("missing123")

    assert result["update_available"] is False
    assert result["error"]


def test_check_image_update_no_config_image(monkeypatch):
    container = FakeContainer(
        attrs={"Config": {"Image": ""}},
        image=FakeImage(tags=[]),
    )
    fake = FakeDockerClient(containers=[container])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)

    result = dm.check_image_update(container.id)

    assert result["update_available"] is False
    assert result["error"] == "No image tag found"


def test_check_image_update_no_local_repo_digests(_patch_container_lookup, monkeypatch):
    container = FakeContainer(attrs={"Config": {"Image": "nginx:latest"}})
    _patch_container_lookup["setup"](container, local_digests=[], remote_info={})

    result = dm.check_image_update(container.id)

    assert result["update_available"] is False
    assert result["error"] == "No local repo digest found"


def test_check_image_update_digests_match(_patch_container_lookup):
    container = FakeContainer(attrs={"Config": {"Image": "nginx:latest"}})
    _patch_container_lookup["setup"](
        container,
        local_digests=["sha256:abc"],
        remote_info={
            "digests": ["sha256:abc", "sha256:child"],
            "index_digest": "sha256:abc",
            "child_digests": ["sha256:child"],
        },
    )

    result = dm.check_image_update(container.id)

    assert result["update_available"] is False
    assert result["local_digest"] == "sha256:abc"


def test_check_image_update_mismatch_index_and_child(_patch_container_lookup):
    container = FakeContainer(attrs={"Config": {"Image": "nginx:latest"}})
    _patch_container_lookup["setup"](
        container,
        local_digests=["sha256:local"],
        remote_info={
            "digests": ["sha256:index", "sha256:child"],
            "index_digest": "sha256:index",
            "child_digests": ["sha256:child"],
        },
    )

    result = dm.check_image_update(container.id)

    assert result["update_available"] is True
    assert result["local_digest"] == "sha256:local"
    assert result["remote_digest"] == "sha256:index"


def test_check_image_update_remote_digests_empty(_patch_container_lookup):
    container = FakeContainer(attrs={"Config": {"Image": "nginx:latest"}})
    _patch_container_lookup["setup"](
        container,
        local_digests=["sha256:local"],
        remote_info={
            "digests": [],
            "index_digest": None,
            "child_digests": [],
        },
    )

    result = dm.check_image_update(container.id)

    assert result["update_available"] is False


def test_check_image_update_index_digest_absent(_patch_container_lookup):
    container = FakeContainer(attrs={"Config": {"Image": "nginx:latest"}})
    _patch_container_lookup["setup"](
        container,
        local_digests=["sha256:local"],
        remote_info={
            "digests": ["sha256:child"],
            "index_digest": None,
            "child_digests": ["sha256:child"],
        },
    )

    result = dm.check_image_update(container.id)

    assert result["update_available"] is False


def test_check_image_update_child_digests_absent(_patch_container_lookup):
    container = FakeContainer(attrs={"Config": {"Image": "nginx:latest"}})
    _patch_container_lookup["setup"](
        container,
        local_digests=["sha256:local"],
        remote_info={
            "digests": ["sha256:index"],
            "index_digest": "sha256:index",
            "child_digests": [],
        },
    )

    result = dm.check_image_update(container.id)

    assert result["update_available"] is False


def test_check_image_update_container_image_from_attrs_not_tags(_patch_container_lookup):
    # The authoritative reference must come from Config.Image, not image.tags.
    container = FakeContainer(attrs={"Config": {"Image": "myapp:1.0"}})
    _patch_container_lookup["setup"](
        container,
        local_digests=["sha256:local"],
        remote_info={
            "digests": ["sha256:remote"],
            "index_digest": "sha256:remote",
            "child_digests": ["sha256:remote"],
        },
    )

    dm.check_image_update(container.id)

    _patch_container_lookup["remote"].assert_called_once_with("myapp", "1.0")


# ---------------------------------------------------------------------------
# _remote_manifest_check
# ---------------------------------------------------------------------------

def test_remote_manifest_check_valid_cache_skips_subprocess(mock_subprocess, mock_docker_client):
    dm._update_check_cache[("nginx", "latest")] = {
        "ts": time.time(),
        "digests": ["sha256:cached"],
        "index_digest": "sha256:cached",
        "child_digests": ["sha256:cached"],
        "media_type": None,
        "platforms": [],
        "error": None,
    }

    result = dm._remote_manifest_check("nginx", "latest")

    mock_subprocess.assert_not_called()
    assert result["digests"] == ["sha256:cached"]


def test_remote_manifest_check_expired_recomputes(mock_subprocess, mock_docker_client):
    dm._update_check_cache[("nginx", "latest")] = {
        "ts": time.time() - 400,
        "digests": ["sha256:old"],
        "index_digest": "sha256:old",
        "child_digests": ["sha256:old"],
        "media_type": None,
        "platforms": [],
        "error": None,
    }
    mock_subprocess.return_value = _completed(
        stdout=(
            '[{"Descriptor": {"digest": "sha256:index"}, '
            '"manifests": [{"digest": "sha256:child"}]}]'
        )
    )

    result = dm._remote_manifest_check("nginx", "latest")

    mock_subprocess.assert_called_once()
    assert "sha256:index" in result["digests"]
    assert "sha256:child" in result["digests"]
    assert result["index_digest"] is None  # fake api returns no distribution info


def test_remote_manifest_check_subprocess_failure_populates_error(mock_subprocess, mock_docker_client):
    mock_subprocess.return_value = _completed(stderr="manifest unknown", returncode=1)

    result = dm._remote_manifest_check("nginx", "latest")

    assert result["error"] == "manifest unknown"
    assert result["digests"] == []
    assert result["child_digests"] == []


def test_remote_manifest_check_invalid_json_no_crash(mock_subprocess, mock_docker_client):
    mock_subprocess.return_value = _completed(stdout="not valid json {{{", returncode=0)

    result = dm._remote_manifest_check("nginx", "latest")

    assert result["child_digests"] == []
    assert result["error"] == "No digest found in manifest inspect output"


def test_remote_manifest_check_oserror(mock_subprocess, mock_docker_client):
    mock_subprocess.side_effect = OSError("docker binary missing")

    result = dm._remote_manifest_check("nginx", "latest")

    assert result["error"] == "docker binary missing"
    assert result["digests"] == []


# ---------------------------------------------------------------------------
# _extract_remote_digests
# ---------------------------------------------------------------------------

def test_extract_remote_digests_manifests():
    payload = [
        {"Descriptor": {"digest": "sha256:index"}},
        {"manifests": [{"digest": "sha256:child1"}, {"digest": "sha256:child2"}]},
    ]
    assert dm._extract_remote_digests(payload) == [
        "sha256:index", "sha256:child1", "sha256:child2",
    ]


def test_extract_remote_digests_nested_schema_v2():
    payload = [
        {
            "Descriptor": {"digest": "sha256:index"},
            "SchemaV2Manifest": {
                "digest": "sha256:child1",
                "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                "config": {"digest": "sha256:config1"},
                "layers": [{"digest": "sha256:layer1"}],
            },
        }
    ]
    assert dm._extract_remote_digests(payload) == ["sha256:index", "sha256:child1"]


def test_extract_remote_digests_deduplicates_preserving_order():
    payload = [
        {"Descriptor": {"digest": "sha256:aaa"}},
        {
            "manifests": [
                {"digest": "sha256:aaa"},
                {"digest": "sha256:bbb"},
                {"digest": "sha256:aaa"},
            ]
        },
        {"digest": "sha256:bbb"},
    ]
    assert dm._extract_remote_digests(payload) == ["sha256:aaa", "sha256:bbb"]


def test_extract_remote_digests_filters_empty_values():
    payload = [
        {"Descriptor": {"digest": ""}},
        {"manifests": [{"digest": None}, {"digest": "sha256:x"}]},
        {"digest": ""},
    ]
    assert dm._extract_remote_digests(payload) == ["sha256:x"]


@pytest.mark.parametrize("payload", [None, {}, [], {"manifests": []}])
def test_extract_remote_digests_empty(payload):
    assert dm._extract_remote_digests(payload) == []


# ---------------------------------------------------------------------------
# _dedupe_preserve_order
# ---------------------------------------------------------------------------

def test_dedupe_preserve_order_basic():
    assert dm._dedupe_preserve_order(["b", "a", "b", "c", "a"]) == ["b", "a", "c"]


def test_dedupe_preserve_order_filters_falsy():
    assert dm._dedupe_preserve_order(["", "x", None, "x"]) == ["x"]
    assert dm._dedupe_preserve_order([]) == []
