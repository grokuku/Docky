"""Tests that silent Docker error handlers still log a warning.

These handlers return empty fallbacks (``[]`` / ``None``) when Docker is
unreachable, but they MUST emit a log so a daemon outage is not masked in
production. The return contract is preserved: we only assert the fallback
value AND the presence of a warning record.
"""

import logging

import pytest
from docker.errors import DockerException

from agent import docker_manager as dm


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKY_DATA_DIR", str(tmp_path))


def test_list_containers_docker_down_logs_warning(tmp_path, monkeypatch, caplog):
    """A failing Docker daemon returns [] but logs a warning."""
    monkeypatch.setattr(
        dm, "get_docker_client",
        lambda: (_ for _ in ()).throw(DockerException("daemon unreachable")),
    )
    with caplog.at_level(logging.WARNING):
        result = dm.list_containers(all=True)
    assert result == []
    assert any(
        "list_containers failed" in r.message
        for r in caplog.records
    )


def test_get_container_full_spec_docker_down_logs_warning(tmp_path, monkeypatch, caplog):
    """A failing container lookup returns None but logs a warning."""
    monkeypatch.setattr(
        dm, "get_docker_client",
        lambda: (_ for _ in ()).throw(DockerException("daemon unreachable")),
    )
    with caplog.at_level(logging.WARNING):
        result = dm._get_container_full_spec("nonexistent")
    assert result is None
    assert any(
        "_get_container_full_spec failed" in r.message and "nonexistent" in r.message
        for r in caplog.records
    )
