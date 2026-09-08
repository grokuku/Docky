"""Tests for the multi-registry authentication on the agent.

Covers the generalization of ``agent/dockerhub.py`` → ``agent/registries.py``:

- ``docker_login`` / ``docker_logout`` for MULTIPLE registries (ghcr.io +
  docker.io coexisting in the same persisted ``config.json``);
- ``list_stored_registries``: hosts with credentials, normalized
  (``https://index.docker.io/v1/`` → ``docker.io``), deduplicated and sorted;
- ``scan_registries``: registry hosts used by the stacks' compose files
  (``image:`` fields, docker's host rule), deduplicated and sorted;
- ``get_registry_auth``: SDK ``auth_config`` picked per the image's registry;
- the new endpoints ``POST /agent/registry/login``,
  ``POST /agent/registry/logout`` and ``GET /agent/registries`` (401 without
  key, payload validation).

Everything is hermetic: subprocess.run is mocked, no real docker daemon, and
the stacks dir is pointed at a temp dir.
"""

import base64
import json
import os
from unittest import mock

import pytest

from agent import registries


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def docker_data_dir(tmp_path, monkeypatch):
    """Point ``registries.get_data_dir`` at a hermetic temp dir."""
    saved_docker_config = os.environ.get("DOCKER_CONFIG")
    monkeypatch.setattr(registries, "get_data_dir", lambda: tmp_path)
    monkeypatch.delenv("DOCKER_CONFIG", raising=False)
    yield tmp_path
    if saved_docker_config is None:
        os.environ.pop("DOCKER_CONFIG", None)
    else:
        os.environ["DOCKER_CONFIG"] = saved_docker_config


@pytest.fixture
def fake_config_dir(docker_data_dir):
    config_dir = docker_data_dir / ".docker"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


@pytest.fixture
def stacks_dir(docker_data_dir, monkeypatch):
    """Point ``registries.get_stacks_dir`` at a hermetic temp dir."""
    stacks = docker_data_dir / "stacks"
    stacks.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(registries, "get_stacks_dir", lambda: stacks)
    return stacks


def _auth(username: str, token: str) -> str:
    return base64.b64encode(f"{username}:{token}".encode("utf-8")).decode("ascii")


def _write_multi_config(config_dir, entries):
    """Write a config.json with several ``auths`` entries.

    *entries* is a dict ``{config_key: {"auth": ...}}``.
    """
    payload = {"auths": entries}
    (config_dir / "config.json").write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# docker_login — multi-registry (ghcr.io + docker.io in the same config)
# ---------------------------------------------------------------------------

def test_docker_login_ghcr_passes_host_and_keeps_dockerhub(docker_data_dir, fake_config_dir):
    """Login to ghcr.io: host passed as CLI arg, token on stdin, Docker Hub
    credentials preserved in the same config.json."""
    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return mock.MagicMock(returncode=0, stdout=b"", stderr=b"")

    # Pre-existing Docker Hub credentials in the same config dir.
    _write_multi_config(fake_config_dir, {
        registries.DOCKER_HUB_REGISTRY: {"auth": _auth("hubuser", "hubtoken")},
    })

    with mock.patch.object(registries.subprocess, "run", side_effect=_fake_run):
        ok, message = registries.docker_login("ghcr.io", "ghuser", "ghcr_pat_secret")

    assert ok is True
    assert "ghcr.io" in message
    cmd = [str(c) for c in captured["cmd"]]
    assert "ghcr.io" in cmd
    assert "--password-stdin" in cmd
    assert "ghcr_pat_secret" not in cmd
    assert captured["kwargs"].get("input") == b"ghcr_pat_secret"
    assert os.environ.get("DOCKER_CONFIG") == str(docker_data_dir / ".docker")


def test_docker_login_dockerhub_no_server_arg(docker_data_dir, fake_config_dir):
    """Default registry (docker.io) → NO server argument (historical behavior)."""
    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return mock.MagicMock(returncode=0, stdout=b"", stderr=b"")

    with mock.patch.object(registries.subprocess, "run", side_effect=_fake_run):
        ok, message = registries.docker_login("", "hubuser", "dckr_pat_secret")

    assert ok is True
    cmd = [str(c) for c in captured["cmd"]]
    assert "docker.io" not in cmd
    assert "index.docker.io" not in cmd
    assert "--password-stdin" in cmd


def test_docker_login_normalizes_dockerhub_alias(docker_data_dir, fake_config_dir):
    """index.docker.io / registry-1.docker.io collapse to docker.io (no arg)."""
    for alias in ("index.docker.io", "registry-1.docker.io", "https://index.docker.io/v1/"):
        captured = {}

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return mock.MagicMock(returncode=0, stdout=b"", stderr=b"")

        with mock.patch.object(registries.subprocess, "run", side_effect=_fake_run):
            ok, _ = registries.docker_login(alias, "u", "t")
        assert ok is True
        cmd = [str(c) for c in captured["cmd"]]
        assert "index.docker.io" not in cmd
        assert "registry-1.docker.io" not in cmd


def test_docker_login_failure_hides_token(docker_data_dir, fake_config_dir):
    with mock.patch.object(
        registries.subprocess,
        "run",
        return_value=mock.MagicMock(returncode=1, stdout=b"", stderr=b"denied: access forbidden"),
    ):
        ok, message = registries.docker_login("ghcr.io", "ghuser", "ghcr_pat_secret")

    assert ok is False
    assert "ghcr_pat_secret" not in message
    assert "denied" in message


# ---------------------------------------------------------------------------
# docker_logout — multi-registry (only the targeted one is removed)
# ---------------------------------------------------------------------------

def test_docker_logout_removes_only_target_registry(docker_data_dir, fake_config_dir):
    """Logout of ghcr.io keeps Docker Hub credentials in the same config.json."""
    _write_multi_config(fake_config_dir, {
        registries.DOCKER_HUB_REGISTRY: {"auth": _auth("hubuser", "hubtoken")},
        "ghcr.io": {"auth": _auth("ghuser", "ghtoken")},
    })
    os.environ["DOCKER_CONFIG"] = str(fake_config_dir)

    with mock.patch.object(
        registries.subprocess, "run", return_value=mock.MagicMock(returncode=0)
    ) as run_mock:
        ok, message = registries.docker_logout("ghcr.io")

    assert ok is True
    assert "ghcr.io" in message
    argv = [str(a) for a in run_mock.call_args[0][0]]
    assert "ghcr.io" in argv
    # config dir still exists (Docker Hub creds remain)
    assert fake_config_dir.exists()
    data = json.loads((fake_config_dir / "config.json").read_text(encoding="utf-8"))
    assert "ghcr.io" not in data["auths"]
    assert registries.DOCKER_HUB_REGISTRY in data["auths"]
    # DOCKER_CONFIG kept because other registries remain
    assert os.environ.get("DOCKER_CONFIG") == str(fake_config_dir)


def test_docker_logout_last_registry_removes_dir(docker_data_dir, fake_config_dir):
    """Logout of the last registry removes the whole config dir (legacy cleanup)."""
    _write_multi_config(fake_config_dir, {
        "ghcr.io": {"auth": _auth("ghuser", "ghtoken")},
    })
    os.environ["DOCKER_CONFIG"] = str(fake_config_dir)

    with mock.patch.object(
        registries.subprocess, "run", return_value=mock.MagicMock(returncode=0)
    ):
        ok, message = registries.docker_logout("ghcr.io")

    assert ok is True
    assert not fake_config_dir.exists()
    assert os.environ.get("DOCKER_CONFIG") is None


# ---------------------------------------------------------------------------
# list_stored_registries — normalization, dedup, sort
# ---------------------------------------------------------------------------

def test_list_stored_registries_normalizes_and_sorts(docker_data_dir, fake_config_dir):
    _write_multi_config(fake_config_dir, {
        registries.DOCKER_HUB_REGISTRY: {"auth": _auth("hubuser", "hubtoken")},
        "ghcr.io": {"auth": _auth("ghuser", "ghtoken")},
        "registry.gitlab.com": {"auth": _auth("gluser", "gltoken")},
        "quay.io": {"auth": _auth("quayuser", "quaytoken")},
    })
    stored = registries.list_stored_registries()
    assert stored == ["docker.io", "ghcr.io", "quay.io", "registry.gitlab.com"]


def test_list_stored_registries_dedups_dockerhub_aliases(docker_data_dir, fake_config_dir):
    """index.docker.io and docker.io both collapse to a single docker.io entry."""
    _write_multi_config(fake_config_dir, {
        registries.DOCKER_HUB_REGISTRY: {"auth": _auth("u1", "t1")},
        "docker.io": {"auth": _auth("u2", "t2")},
        "index.docker.io": {"auth": _auth("u3", "t3")},
    })
    assert registries.list_stored_registries() == ["docker.io"]


def test_list_stored_registries_skips_empty_entries(docker_data_dir, fake_config_dir):
    _write_multi_config(fake_config_dir, {
        "ghcr.io": {"auth": _auth("u", "t")},
        "quay.io": {},  # no auth blob → skipped
    })
    assert registries.list_stored_registries() == ["ghcr.io"]


def test_list_stored_registries_empty_without_config(docker_data_dir):
    assert registries.list_stored_registries() == []


# ---------------------------------------------------------------------------
# scan_registries — compose image: fields, docker host rule, dedup, sort
# ---------------------------------------------------------------------------

def _write_compose(stacks_dir, stack_name, services):
    stack = stacks_dir / stack_name
    stack.mkdir(parents=True, exist_ok=True)
    (stack / "docker-compose.yml").write_text(
        json.dumps({"services": services}), encoding="utf-8"
    )


def test_scan_registries_extracts_hosts_and_dedups(stacks_dir):
    _write_compose(stacks_dir, "stack-a", {
        "web": {"image": "ghcr.io/org/web:latest"},
        "db": {"image": "postgres:16"},
        "cache": {"image": "lscr.io/linuxserver/redis:latest"},
    })
    _write_compose(stacks_dir, "stack-b", {
        "app": {"image": "registry.gitlab.com/group/app:v1"},
        "web2": {"image": "ghcr.io/org/web:1.0"},  # duplicate host
        "plain": {"image": "nginx"},  # no prefix → docker.io
    })
    discovered = registries.scan_registries()
    assert discovered == ["docker.io", "ghcr.io", "lscr.io", "registry.gitlab.com"]


def test_scan_registries_handles_tags_and_digests(stacks_dir):
    _write_compose(stacks_dir, "stack-a", {
        "a": {"image": "ghcr.io/org/app:1.2.3"},
        "b": {"image": "quay.io/org/tool@sha256:abc123"},
        "c": {"image": "user/app"},  # Docker Hub namespace, no host
    })
    assert registries.scan_registries() == ["docker.io", "ghcr.io", "quay.io"]


def test_scan_registries_skips_interpolation_and_invalid(stacks_dir):
    _write_compose(stacks_dir, "stack-a", {
        "a": {"image": "${REGISTRY}/app"},
        "b": {"image": "ghcr.io/org/app"},
        "c": {"image": 123},  # not a string → skipped
    })
    assert registries.scan_registries() == ["ghcr.io"]


def test_scan_registries_empty_without_stacks(docker_data_dir, monkeypatch):
    empty = docker_data_dir / "nostacks"
    empty.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(registries, "get_stacks_dir", lambda: empty)
    assert registries.scan_registries() == []


def test_scan_registries_skips_unreadable_compose(stacks_dir):
    stack = stacks_dir / "bad"
    stack.mkdir(parents=True, exist_ok=True)
    (stack / "docker-compose.yml").write_text("{not yaml", encoding="utf-8")
    _write_compose(stacks_dir, "good", {"a": {"image": "ghcr.io/org/app"}})
    assert registries.scan_registries() == ["ghcr.io"]


# ---------------------------------------------------------------------------
# get_registry_auth — per-image registry
# ---------------------------------------------------------------------------

def test_get_registry_auth_picks_by_image_registry(docker_data_dir, fake_config_dir):
    _write_multi_config(fake_config_dir, {
        registries.DOCKER_HUB_REGISTRY: {"auth": _auth("hubuser", "hubtoken")},
        "ghcr.io": {"auth": _auth("ghuser", "ghtoken")},
        "registry.gitlab.com": {"auth": _auth("gluser", "gltoken")},
    })
    assert registries.get_registry_auth("ghcr.io/org/app:latest") == {
        "username": "ghuser", "password": "ghtoken",
    }
    assert registries.get_registry_auth("registry.gitlab.com/group/app") == {
        "username": "gluser", "password": "gltoken",
    }
    # no prefix → Docker Hub
    assert registries.get_registry_auth("nginx:latest") == {
        "username": "hubuser", "password": "hubtoken",
    }
    # explicit docker.io prefix → Docker Hub
    assert registries.get_registry_auth("docker.io/library/nginx") == {
        "username": "hubuser", "password": "hubtoken",
    }


def test_get_registry_auth_none_for_unknown_registry(docker_data_dir, fake_config_dir):
    _write_multi_config(fake_config_dir, {
        "ghcr.io": {"auth": _auth("ghuser", "ghtoken")},
    })
    # quay.io has no stored creds → None (anonymous pull)
    assert registries.get_registry_auth("quay.io/org/app") is None


def test_get_registry_auth_identitytoken(docker_data_dir, fake_config_dir):
    _write_multi_config(fake_config_dir, {
        "ghcr.io": {"identitytoken": "some-jwt-token"},
    })
    assert registries.get_registry_auth("ghcr.io/org/app") == {
        "identitytoken": "some-jwt-token",
    }


# ---------------------------------------------------------------------------
# Endpoints — POST /agent/registry/login, /logout, GET /agent/registries
# ---------------------------------------------------------------------------

def test_registry_login_endpoint_success(agent_client, api_key_header, monkeypatch):
    recorded = {}

    def _fake_login(registry, username, token):
        recorded["registry"] = registry
        recorded["username"] = username
        recorded["token"] = token
        return True, "Connecté à ghcr.io"

    monkeypatch.setattr(registries, "docker_login", _fake_login)

    resp = agent_client.post(
        "/agent/registry/login",
        headers=api_key_header,
        json={"registry": "ghcr.io", "username": "ghuser", "token": "ghtoken"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["registry"] == "ghcr.io"
    assert recorded == {"registry": "ghcr.io", "username": "ghuser", "token": "ghtoken"}


def test_registry_login_endpoint_requires_api_key(agent_client):
    resp = agent_client.post(
        "/agent/registry/login",
        json={"registry": "ghcr.io", "username": "u", "token": "t"},
    )
    assert resp.status_code == 401


def test_registry_login_endpoint_missing_credentials_400(agent_client, api_key_header):
    resp = agent_client.post(
        "/agent/registry/login",
        headers=api_key_header,
        json={"registry": "ghcr.io", "username": "", "token": ""},
    )
    assert resp.status_code == 400
    assert "username et token" in resp.json()["error"]


def test_registry_login_endpoint_invalid_registry_400(agent_client, api_key_header):
    resp = agent_client.post(
        "/agent/registry/login",
        headers=api_key_header,
        json={"registry": "not a host/with path", "username": "u", "token": "t"},
    )
    assert resp.status_code == 400
    assert "registry invalide" in resp.json()["error"]


def test_registry_login_endpoint_failure_502(agent_client, api_key_header, monkeypatch):
    monkeypatch.setattr(
        registries, "docker_login",
        lambda r, u, t: (False, "docker login a échoué: denied"),
    )
    resp = agent_client.post(
        "/agent/registry/login",
        headers=api_key_header,
        json={"registry": "ghcr.io", "username": "u", "token": "bad"},
    )
    assert resp.status_code == 502
    assert resp.json()["success"] is False


def test_registry_logout_endpoint_success(agent_client, api_key_header, monkeypatch):
    recorded = []

    def _fake_logout(registry):
        recorded.append(registry)
        return True, "Déconnecté de ghcr.io"

    monkeypatch.setattr(registries, "docker_logout", _fake_logout)

    resp = agent_client.post(
        "/agent/registry/logout",
        headers=api_key_header,
        json={"registry": "ghcr.io"},
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert recorded == ["ghcr.io"]


def test_registry_logout_endpoint_requires_api_key(agent_client):
    resp = agent_client.post("/agent/registry/logout", json={"registry": "ghcr.io"})
    assert resp.status_code == 401


def test_registries_endpoint_lists_stored_and_discovered(
    agent_client, api_key_header, monkeypatch
):
    monkeypatch.setattr(registries, "list_stored_registries", lambda: ["docker.io", "ghcr.io"])
    monkeypatch.setattr(registries, "scan_registries", lambda: ["docker.io", "lscr.io"])

    resp = agent_client.get("/agent/registries", headers=api_key_header)
    assert resp.status_code == 200
    assert resp.json() == {
        "stored": ["docker.io", "ghcr.io"],
        "discovered": ["docker.io", "lscr.io"],
    }


def test_registries_endpoint_requires_api_key(agent_client):
    resp = agent_client.get("/agent/registries")
    assert resp.status_code == 401
