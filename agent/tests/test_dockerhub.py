"""Tests for Docker Hub authentication on the agent.

Covers:
- ``agent.dockerhub.docker_login``: token passed on STDIN (--password-stdin),
  NEVER in argv; failure message never contains the token.
- ``agent.dockerhub.docker_logout``: best-effort logout + config dir removal.
- ``agent.dockerhub.apply_persisted_config``: DOCKER_CONFIG re-injection at
  startup when <data_dir>/.docker/config.json exists.
- ``agent.dockerhub.get_registry_auth``: SDK auth_config decoding.
- ``POST /agent/dockerhub/login`` endpoint: success / 401 without key /
  400 with missing credentials / disabled → logout.
- Agent startup re-injects the persisted config.

Everything is hermetic: subprocess.run is mocked, no real docker daemon.
"""

import base64
import json
import os
from unittest import mock

import pytest

from agent import dockerhub


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def docker_data_dir(tmp_path, monkeypatch):
    """Point ``dockerhub.get_data_dir`` at a hermetic temp dir.

    ``dockerhub`` imports ``get_data_dir`` from ``agent.config`` into its own
    namespace, so the symbol must be patched there. ``DOCKER_CONFIG`` is
    snapshotted/restored because docker_login/logout mutate os.environ.
    """
    saved_docker_config = os.environ.get("DOCKER_CONFIG")
    monkeypatch.setattr(dockerhub, "get_data_dir", lambda: tmp_path)
    monkeypatch.delenv("DOCKER_CONFIG", raising=False)
    yield tmp_path
    if saved_docker_config is None:
        os.environ.pop("DOCKER_CONFIG", None)
    else:
        os.environ["DOCKER_CONFIG"] = saved_docker_config


@pytest.fixture
def fake_config_dir(docker_data_dir):
    """Create <data_dir>/.docker and return its path."""
    config_dir = docker_data_dir / ".docker"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def _write_config(config_dir: dict, username: str = "hubuser", token: str = "dckr_pat_abc123"):
    """Write a config.json like the one produced by ``docker login``."""
    auth = base64.b64encode(f"{username}:{token}".encode("utf-8")).decode("ascii")
    payload = {"auths": {dockerhub.DOCKER_HUB_REGISTRY: {"auth": auth}}}
    (config_dir / "config.json").write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# docker_login — token on STDIN, never in argv
# ---------------------------------------------------------------------------

def test_docker_login_passes_token_via_stdin_not_argv(docker_data_dir, fake_config_dir):
    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return mock.MagicMock(returncode=0, stdout=b"", stderr=b"")

    with mock.patch.object(dockerhub.subprocess, "run", side_effect=_fake_run):
        ok, message = dockerhub.docker_login("hubuser", "dckr_pat_secret")

    assert ok is True
    assert "Connecté" in message
    cmd = [str(c) for c in captured["cmd"]]
    # --password-stdin present, token NOT in argv
    assert "--password-stdin" in cmd
    assert "dckr_pat_secret" not in cmd
    assert "dckr_pat_secret" not in " ".join(cmd)
    # username in argv (-u hubuser)
    assert "hubuser" in cmd
    # token travelled via stdin input
    assert captured["kwargs"].get("input") == b"dckr_pat_secret"
    # --config points at the persistent data dir
    assert "--config" in cmd
    assert str(docker_data_dir / ".docker") in cmd
    # successful login exports DOCKER_CONFIG for subsequent subprocesses
    assert os.environ.get("DOCKER_CONFIG") == str(docker_data_dir / ".docker")


def test_docker_login_failure_message_hides_token(docker_data_dir, fake_config_dir):
    with mock.patch.object(
        dockerhub.subprocess,
        "run",
        return_value=mock.MagicMock(returncode=1, stdout=b"", stderr=b"unauthorized: incorrect username or password"),
    ):
        ok, message = dockerhub.docker_login("hubuser", "dckr_pat_secret")

    assert ok is False
    assert "docker login a échoué" in message
    assert "dckr_pat_secret" not in message
    assert "unauthorized" in message
    # no env export on failure
    assert os.environ.get("DOCKER_CONFIG") is None


def test_docker_login_oserror_reported_without_token(docker_data_dir, fake_config_dir):
    with mock.patch.object(
        dockerhub.subprocess, "run", side_effect=OSError("docker binary missing")
    ):
        ok, message = dockerhub.docker_login("hubuser", "dckr_pat_secret")

    assert ok is False
    assert "docker binary missing" in message
    assert "dckr_pat_secret" not in message


# ---------------------------------------------------------------------------
# docker_logout — cleanup of the config dir
# ---------------------------------------------------------------------------

def test_docker_logout_removes_config_dir(docker_data_dir, fake_config_dir):
    _write_config(fake_config_dir)
    os.environ["DOCKER_CONFIG"] = str(fake_config_dir)
    with mock.patch.object(
        dockerhub.subprocess, "run", return_value=mock.MagicMock(returncode=0)
    ) as run_mock:
        ok, message = dockerhub.docker_logout()

    assert ok is True
    assert "Déconnecté" in message
    run_mock.assert_called_once()
    argv = [str(a) for a in run_mock.call_args[0][0]]
    assert "logout" in argv
    assert not fake_config_dir.exists()
    assert os.environ.get("DOCKER_CONFIG") is None


def test_docker_logout_noop_without_dir(docker_data_dir):
    ok, message = dockerhub.docker_logout()
    assert ok is True
    assert "Déconnecté" in message


# ---------------------------------------------------------------------------
# apply_persisted_config — DOCKER_CONFIG re-injection at startup
# ---------------------------------------------------------------------------

def test_apply_persisted_config_sets_docker_config(docker_data_dir):
    config_dir = docker_data_dir / ".docker"
    config_dir.mkdir(parents=True)
    _write_config(config_dir)

    assert dockerhub.apply_persisted_config() is True
    assert os.environ.get("DOCKER_CONFIG") == str(config_dir)


def test_apply_persisted_config_noop_without_file(docker_data_dir):
    assert dockerhub.apply_persisted_config() is False
    assert os.environ.get("DOCKER_CONFIG") is None


def test_agent_startup_reapplies_persisted_config(agent_client, monkeypatch):
    """The agent's startup event calls ``apply_persisted_config``."""
    calls = []
    monkeypatch.setattr(dockerhub, "apply_persisted_config", lambda: calls.append(1) or False)
    from fastapi.testclient import TestClient
    from agent.main import app

    with TestClient(app):
        pass
    assert calls, "startup should re-inject the persisted docker config"


# ---------------------------------------------------------------------------
# get_registry_auth — SDK auth_config decoding
# ---------------------------------------------------------------------------

def test_get_registry_auth_decodes_config(docker_data_dir):
    config_dir = docker_data_dir / ".docker"
    config_dir.mkdir(parents=True)
    _write_config(config_dir, username="hubuser", token="dckr_pat_secret")

    auth = dockerhub.get_registry_auth()
    assert auth == {"username": "hubuser", "password": "dckr_pat_secret"}


def test_get_registry_auth_none_without_config(docker_data_dir):
    assert dockerhub.get_registry_auth() is None


def test_get_registry_auth_none_on_garbage(docker_data_dir):
    config_dir = docker_data_dir / ".docker"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text("{not json", encoding="utf-8")
    assert dockerhub.get_registry_auth() is None

    (config_dir / "config.json").write_text(json.dumps({"auths": {}}), encoding="utf-8")
    assert dockerhub.get_registry_auth() is None


# ---------------------------------------------------------------------------
# POST /agent/dockerhub/login endpoint
# ---------------------------------------------------------------------------

def test_dockerhub_endpoint_login_success(agent_client, api_key_header, monkeypatch):
    recorded = {}

    def _fake_login(username, token):
        recorded["username"] = username
        recorded["token"] = token
        return True, "Connecté à Docker Hub"

    monkeypatch.setattr(dockerhub, "docker_login", _fake_login)

    resp = agent_client.post(
        "/agent/dockerhub/login",
        headers=api_key_header,
        json={"username": "hubuser", "token": "dckr_pat_secret", "enabled": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["message"] == "Connecté à Docker Hub"
    assert recorded == {"username": "hubuser", "token": "dckr_pat_secret"}


def test_dockerhub_endpoint_requires_api_key(agent_client):
    resp = agent_client.post(
        "/agent/dockerhub/login",
        json={"username": "hubuser", "token": "dckr_pat_secret", "enabled": True},
    )
    assert resp.status_code == 401


def test_dockerhub_endpoint_bad_key_401(agent_client):
    resp = agent_client.post(
        "/agent/dockerhub/login",
        headers={"Authorization": "Bearer wrong-key"},
        json={"username": "hubuser", "token": "t", "enabled": True},
    )
    assert resp.status_code == 401


def test_dockerhub_endpoint_missing_credentials_400(agent_client, api_key_header):
    resp = agent_client.post(
        "/agent/dockerhub/login",
        headers=api_key_header,
        json={"username": "", "token": "", "enabled": True},
    )
    assert resp.status_code == 400
    assert "username et token" in resp.json()["error"]


def test_dockerhub_endpoint_disabled_triggers_logout(agent_client, api_key_header, monkeypatch):
    logout_calls = []

    def _fake_logout():
        logout_calls.append(1)
        return True, "Déconnecté de Docker Hub"

    monkeypatch.setattr(dockerhub, "docker_logout", _fake_logout)

    resp = agent_client.post(
        "/agent/dockerhub/login",
        headers=api_key_header,
        json={"enabled": False},
    )
    assert resp.status_code == 200
    assert resp.json() == {"success": True, "message": "Déconnecté de Docker Hub"}
    assert logout_calls == [1]


def test_dockerhub_endpoint_login_failure_502(agent_client, api_key_header, monkeypatch):
    monkeypatch.setattr(
        dockerhub, "docker_login", lambda u, t: (False, "docker login a échoué: unauthorized")
    )
    resp = agent_client.post(
        "/agent/dockerhub/login",
        headers=api_key_header,
        json={"username": "hubuser", "token": "bad", "enabled": True},
    )
    assert resp.status_code == 502
    body = resp.json()
    assert body["success"] is False
    assert "unauthorized" in body["message"]