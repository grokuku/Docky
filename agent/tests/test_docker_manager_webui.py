"""Tests for the "WebUI par container" feature (docky.webui.* labels).

Covers:
- parsing of ``docky.webui.*`` labels (1, several, with/without name, order);
- the ``webui`` field exposed in ``_container_to_dict`` and
  ``_get_container_full_spec``;
- writing the labels into a managed compose service (old ones replaced);
- applying the labels on a standalone container recreate;
- option B is intentionally NOT resolved on the agent (raw URL returned).

Hermetic: the data dir is redirected to ``tmp_path``, git helpers are stubbed,
and ``compose_up`` / ``_recreate_container`` are faked so no Docker daemon is
touched.
"""

from pathlib import Path

import pytest

from agent import docker_manager as dm
from agent.tests.conftest import FakeContainer, FakeDockerClient

COMPOSE = """\
services:
  web:
    image: nginx:latest
    labels:
      docky.webui.1: http://old.example
      docky.webui.1.name: Old
      keep.me: yes
"""


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(dm, "_git_init", lambda: None)
    monkeypatch.setattr(dm, "_git_save", lambda *args, **kwargs: None)


def _managed_stack(tmp_path: Path, name: str = "myapp") -> Path:
    stack_dir = Path(tmp_path) / "stacks" / name
    stack_dir.mkdir(parents=True, exist_ok=True)
    (stack_dir / "docker-compose.yml").write_text(COMPOSE, encoding="utf-8")
    return stack_dir


def _stack_container(container_id: str = "abc1"):
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
    attrs = {"Config": {"Image": "nginx:latest", "Labels": {}}}
    return FakeContainer(container_id="solo1", name="web", attrs=attrs)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_parse_single_without_name():
    labels = {"docky.webui.1": "http://a.example"}
    assert dm._parse_webui_labels(labels) == [{"url": "http://a.example"}]


def test_parse_multiple_sorted_with_names():
    labels = {
        "docky.webui.2": "http://b.example",
        "docky.webui.1": "http://a.example",
        "docky.webui.1.name": "Admin",
        "docky.webui.3": ":8080",
    }
    assert dm._parse_webui_labels(labels) == [
        {"url": "http://a.example", "name": "Admin"},
        {"url": "http://b.example"},
        {"url": ":8080"},
    ]


def test_parse_ignores_empty_and_other_labels():
    labels = {
        "docky.webui.1": "   ",
        "docky.webui.2": "http://ok.example",
        "other": "x",
    }
    assert dm._parse_webui_labels(labels) == [{"url": "http://ok.example"}]


def test_parse_returns_raw_url_option_b_not_resolved():
    """The agent returns the raw URL; option B is resolved on the frontend."""
    labels = {"docky.webui.1": ":8080"}
    assert dm._parse_webui_labels(labels) == [{"url": ":8080"}]


def test_webui_labels_from_spec():
    spec = [{"url": "http://a", "name": "Admin"}, {"url": ":8080"}, {"url": "  "}]
    assert dm._webui_labels_from_spec(spec) == {
        "docky.webui.1": "http://a",
        "docky.webui.1.name": "Admin",
        "docky.webui.2": ":8080",
    }


# ---------------------------------------------------------------------------
# _container_to_dict exposes webui
# ---------------------------------------------------------------------------

def test_container_to_dict_exposes_webui():
    attrs = {
        "Config": {
            "Image": "nginx:latest",
            "Labels": {
                "docky.webui.1": "http://a.example",
                "docky.webui.1.name": "Admin",
            },
        },
        "State": {},
        "Created": "",
    }
    c = FakeContainer(container_id="abc1", name="web", attrs=attrs)
    d = dm._container_to_dict(c)
    assert d["webui"] == [{"url": "http://a.example", "name": "Admin"}]


def test_container_to_dict_webui_empty():
    c = FakeContainer(container_id="abc1", name="web")
    d = dm._container_to_dict(c)
    assert d["webui"] == []


def test_get_container_full_spec_exposes_webui():
    attrs = {
        "Config": {
            "Image": "nginx:latest",
            "Labels": {
                "docky.webui.1": "http://a.example",
                "docky.webui.1.name": "Admin",
            },
            "Env": [],
        },
        "State": {},
        "NetworkSettings": {"Ports": {}, "Networks": {}},
        "Mounts": [],
        "HostConfig": {"RestartPolicy": {"Name": "no"}},
    }
    c = FakeContainer(container_id="abc1", name="web", attrs=attrs)
    fake = FakeDockerClient(containers=[c])
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)
    try:
        spec = dm._get_container_full_spec("abc1")
    finally:
        monkeypatch.undo()
    assert spec["webui"] == [{"url": "http://a.example", "name": "Admin"}]


# ---------------------------------------------------------------------------
# Compose write (managed stack)
# ---------------------------------------------------------------------------

async def test_compose_update_writes_webui_labels(tmp_path, monkeypatch):
    stack_dir = _managed_stack(tmp_path)

    fake = FakeDockerClient(containers=[_stack_container()])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)
    monkeypatch.setattr(dm, "compose_up", lambda stack: __import__("asyncio").sleep(0) or {"success": True})

    spec = {
        "name": "web",
        "image": "nginx:latest",
        "restart_policy": "no",
        "ports": [],
        "volumes": [],
        "env": [],
        "webui": [
            {"url": "http://new.example", "name": "Nouveau"},
            {"url": ":8080"},
        ],
    }

    result = await dm.update_container("abc1", spec)
    assert result["success"] is True

    import yaml
    compose = yaml.safe_load((stack_dir / "docker-compose.yml").read_text(encoding="utf-8"))
    labels = compose["services"]["web"]["labels"]
    # Old webui labels replaced, non-webui label preserved.
    assert labels["docky.webui.1"] == "http://new.example"
    assert labels["docky.webui.1.name"] == "Nouveau"
    assert labels["docky.webui.2"] == ":8080"
    assert "docky.webui.1.name" in labels
    assert "keep.me" in labels
    assert "Old" not in labels.values()


async def test_compose_update_clears_webui_labels(tmp_path, monkeypatch):
    stack_dir = _managed_stack(tmp_path)

    fake = FakeDockerClient(containers=[_stack_container()])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)
    monkeypatch.setattr(dm, "compose_up", lambda stack: __import__("asyncio").sleep(0) or {"success": True})

    spec = {
        "name": "web",
        "image": "nginx:latest",
        "restart_policy": "no",
        "ports": [],
        "volumes": [],
        "env": [],
        "webui": [],
    }

    result = await dm.update_container("abc1", spec)
    assert result["success"] is True

    import yaml
    compose = yaml.safe_load((stack_dir / "docker-compose.yml").read_text(encoding="utf-8"))
    labels = compose["services"]["web"]["labels"]
    assert not any(k.startswith("docky.webui.") for k in labels)
    assert "keep.me" in labels


# ---------------------------------------------------------------------------
# Standalone recreate
# ---------------------------------------------------------------------------

async def test_standalone_recreate_applies_webui_labels(tmp_path, monkeypatch):
    fake = FakeDockerClient(containers=[_standalone_container()])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)

    captured = {}
    async def fake_recreate(c, container_id, spec, client, attrs):
        captured["spec"] = spec
        return {"success": True, "output": "recreated"}
    monkeypatch.setattr(dm, "_recreate_container", fake_recreate)

    spec = {
        "name": "web",
        "image": "nginx:latest",
        "restart_policy": "no",
        "ports": [],
        "volumes": [],
        "env": [],
        "webui": [{"url": "http://a.example", "name": "Admin"}],
    }

    result = await dm.update_container("solo1", spec)
    assert result["success"] is True
    assert captured["spec"]["webui"] == [{"url": "http://a.example", "name": "Admin"}]


async def test_standalone_recreate_merges_webui_into_labels(tmp_path, monkeypatch):
    """The recreate path must merge webui labels into the run label set."""
    from agent.docker_manager import _webui_labels_from_spec
    labels = _webui_labels_from_spec([{"url": "http://a.example", "name": "Admin"}])
    assert labels == {
        "docky.webui.1": "http://a.example",
        "docky.webui.1.name": "Admin",
    }
