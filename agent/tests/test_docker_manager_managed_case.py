"""Tests for case-insensitive managed-stack recognition (case bug fix).

Docker always lowercases the ``com.docker.compose.project`` label, while the
directory Docky created may keep its original casing (e.g. ``MyApp`` vs the
``myapp`` label).  A case-sensitive lookup would wrongly report such a stack
as *external* — or, worse, create a second directory with a mismatched case
when writing the compose / git save.

These tests verify the single case-insensitive helper
``_find_managed_stack_dir`` is used in ``_get_container_full_spec`` and
``update_container``, and that real external stacks (no managed directory),
ambiguous directory-case collisions, and plain dirs all behave without side
effects.

Hermetic: the data dir is redirected to ``tmp_path``, the git helpers are
stubbed (no real git repository), and ``compose_up`` is faked so no Docker
daemon is touched.
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


def _managed_stack(tmp_path: Path, name: str) -> Path:
    """Create a managed stack directory (with a compose file) and return it."""
    stack_dir = Path(tmp_path) / "stacks" / name
    stack_dir.mkdir(parents=True, exist_ok=True)
    (stack_dir / "docker-compose.yml").write_text(COMPOSE, encoding="utf-8")
    return stack_dir


def _stack_container(project: str = "myapp", container_id: str = "abc1"):
    """A container whose (Docker-lowercased) compose project is *project*."""
    attrs = {
        "Config": {
            "Image": "nginx:latest",
            "Labels": {
                "com.docker.compose.project": project,
                "com.docker.compose.service": "web",
            },
            "Env": [],
        },
        "State": {},
        "NetworkSettings": {"Ports": {}, "Networks": {}},
        "Mounts": [],
        "HostConfig": {"RestartPolicy": {"Name": "no"}},
        "Created": "",
    }
    return FakeContainer(container_id=container_id, name="web", attrs=attrs)


def _edit_spec() -> dict:
    return {
        "name": "web",
        "image": "nginx:1.25",
        "restart_policy": "no",
        "ports": [],
        "volumes": [],
        "env": [],
    }


# ---------------------------------------------------------------------------
# helper unique insensible à la casse (chemin RÉEL résolu)
# ---------------------------------------------------------------------------

def test_find_managed_stack_dir_case_insensitive(tmp_path):
    _managed_stack(tmp_path, "MyApp")
    path = dm._find_managed_stack_dir("myapp")
    assert path is not None
    # Le chemin RÉEL (casse d'origine) est renvoyé.
    assert path.name == "MyApp"
    assert path.exists()


def test_find_managed_stack_dir_exact_wins_on_collision(tmp_path):
    """myapp + MyApp : la correspondance exacte gagne, pas de crash."""
    _managed_stack(tmp_path, "MyApp")
    _managed_stack(tmp_path, "myapp")
    path = dm._find_managed_stack_dir("myapp")
    assert path is not None
    assert path.name == "myapp"


def test_find_managed_stack_dir_ambiguous_collision_returns_none(tmp_path):
    """MyApp + MYAPP (label myapp) : ambigu → None, pas de devinette."""
    _managed_stack(tmp_path, "MyApp")
    _managed_stack(tmp_path, "MYAPP")
    assert dm._find_managed_stack_dir("myapp") is None


def test_find_managed_stack_dir_no_dir_or_no_match(tmp_path):
    assert dm._find_managed_stack_dir("myapp") is None
    _managed_stack(tmp_path, "other")
    assert dm._find_managed_stack_dir("myapp") is None
    # project vide → None également.
    assert dm._find_managed_stack_dir("") is None


# ---------------------------------------------------------------------------
# _get_container_full_spec (point 710)
# ---------------------------------------------------------------------------

def test_full_spec_managed_true_with_different_case_dir(tmp_path, monkeypatch):
    """Dossier ``MyApp`` + label ``myapp`` → managed True, stack = ``MyApp``."""
    _managed_stack(tmp_path, "MyApp")

    fake = FakeDockerClient(containers=[_stack_container(project="myapp")])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)

    spec = dm._get_container_full_spec("abc1")
    assert spec is not None
    assert spec["managed"] is True
    # La casse réelle du dossier est restituée (pas la casse du label).
    assert spec["stack"] == "MyApp"


def test_full_spec_managed_false_for_real_external(tmp_path, monkeypatch):
    """Label sans dossier géré → managed False (stack externe)."""
    _managed_stack(tmp_path, "other")

    fake = FakeDockerClient(containers=[_stack_container(project="myapp")])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)

    # Aucun dossier ne matche la casse ``myapp`` → externe.
    spec = dm._get_container_full_spec("abc1")
    assert spec is not None
    assert spec["managed"] is False


# ---------------------------------------------------------------------------
# update_container (points 1461-1462) — écriture compose / git save
# ---------------------------------------------------------------------------

async def test_update_container_managed_with_different_case_dir(tmp_path, monkeypatch):
    """Dossier ``MyApp`` + label ``myapp`` → édition fonctionnelle, écrit dans
    le dossier RÉEL (aucun second dossier ``myapp`` créé)."""
    stack_dir = _managed_stack(tmp_path, "MyApp")

    fake = FakeDockerClient(containers=[_stack_container(project="myapp")])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)

    git_calls = []
    monkeypatch.setattr(dm, "_git_save", lambda stack, msg=None: git_calls.append((stack, msg)))
    monkeypatch.setattr(dm, "compose_up", lambda stack: __import__("asyncio").sleep(0) or {"success": True})

    result = await dm.update_container("abc1", _edit_spec())

    assert result["success"] is True
    # Écrit dans le dossier d'origine (casse réelle), pas un nouveau ``myapp``.
    assert "nginx:1.25" in (stack_dir / "docker-compose.yml").read_text(encoding="utf-8")
    assert not (Path(tmp_path) / "stacks" / "myapp").exists()
    # git save sur le nom réel.
    assert git_calls and git_calls[0][0] == "MyApp"


async def test_update_container_label_without_managed_dir_refused(tmp_path, monkeypatch):
    """Label ``myapp`` sans dossier géré → refusé (message 1464 inchangé)."""
    _managed_stack(tmp_path, "other")

    fake = FakeDockerClient(containers=[_stack_container(project="myapp")])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)

    result = await dm.update_container("abc1", _edit_spec())

    assert result["success"] is False
    assert result["error"] == dm.EXTERNAL_STACK_EDIT_REFUSED


async def test_update_container_case_collision_no_crash(tmp_path, monkeypatch):
    """Collision de casse (MyApp + myapp) : pas de crash, résolue proprement."""
    _managed_stack(tmp_path, "MyApp")
    _managed_stack(tmp_path, "myapp")

    fake = FakeDockerClient(containers=[_stack_container(project="myapp")])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)
    monkeypatch.setattr(dm, "compose_up", lambda stack: __import__("asyncio").sleep(0) or {"success": True})

    # La correspondance exacte ``myapp`` gagne : édition sur ce dossier.
    result = await dm.update_container("abc1", _edit_spec())
    assert result["success"] is True
    assert "nginx:1.25" in (Path(tmp_path) / "stacks" / "myapp" / "docker-compose.yml").read_text(encoding="utf-8")


async def test_update_container_ambiguous_collision_refused_no_crash(tmp_path, monkeypatch):
    """MyApp + MYAPP (label myapp) : ambigu → refusé proprement, pas de crash."""
    _managed_stack(tmp_path, "MyApp")
    _managed_stack(tmp_path, "MYAPP")

    fake = FakeDockerClient(containers=[_stack_container(project="myapp")])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)

    result = await dm.update_container("abc1", _edit_spec())
    assert result["success"] is False
    assert result["error"] == dm.EXTERNAL_STACK_EDIT_REFUSED


# ---------------------------------------------------------------------------
# autoFix « par emplacement du compose » (working_dir/config_files dans stacks)
# ---------------------------------------------------------------------------

OVERRIDE_COMPOSE = """\
name: myproject
services:
  web:
    image: nginx:latest
    ports:
      - "8080:80"
"""


def _override_stack_container(project="myproject", working_dir=None, config_files=None):
    """A container whose compose project label is overridden (``name:``) so it
    no longer matches the folder name, but whose working_dir/config_files point
    into the managed folder."""
    labels = {
        "com.docker.compose.project": project,
        "com.docker.compose.service": "web",
    }
    if working_dir:
        labels["com.docker.compose.project.working_dir"] = working_dir
    if config_files:
        labels["com.docker.compose.project.config_files"] = config_files
    attrs = {
        "Config": {"Image": "nginx:latest", "Labels": labels, "Env": []},
        "State": {},
        "NetworkSettings": {"Ports": {}, "Networks": {}},
        "Mounts": [],
        "HostConfig": {"RestartPolicy": {"Name": "no"}},
        "Created": "",
    }
    return FakeContainer(container_id="abc1", name="web", attrs=attrs)


def _override_managed_stack(tmp_path: Path, name: str) -> Path:
    """Create a managed stack whose compose declares a ``name:`` override."""
    stack_dir = Path(tmp_path) / "stacks" / name
    stack_dir.mkdir(parents=True, exist_ok=True)
    (stack_dir / "docker-compose.yml").write_text(OVERRIDE_COMPOSE, encoding="utf-8")
    return stack_dir


def test_resolve_managed_stack_by_location_override_name(tmp_path):
    """Label surchargé (name:) + working_dir dans stacks → managé par emplacement."""
    _override_managed_stack(tmp_path, "MyApp")
    labels = {
        "com.docker.compose.project": "myproject",
        "com.docker.compose.project.working_dir": str(Path(tmp_path) / "stacks" / "MyApp"),
        "com.docker.compose.project.config_files": str(Path(tmp_path) / "stacks" / "MyApp" / "docker-compose.yml"),
    }
    resolved = dm._resolve_managed_stack(labels)
    assert resolved is not None
    # Le nom RÉEL du dossier est restitué (casse d'origine).
    assert resolved[0] == "MyApp"
    assert Path(resolved[1]).name == "MyApp"


def test_resolve_managed_stack_by_location_config_files_only(tmp_path):
    """Sans working_dir, le config_files (parent) suffit à localiser le dossier."""
    _override_managed_stack(tmp_path, "MyApp")
    labels = {
        "com.docker.compose.project": "myproject",
        "com.docker.compose.project.config_files": str(Path(tmp_path) / "stacks" / "MyApp" / "docker-compose.yml"),
    }
    resolved = dm._resolve_managed_stack(labels)
    assert resolved is not None
    assert resolved[0] == "MyApp"


def test_resolve_managed_stack_external_working_dir_outside(tmp_path):
    """working_dir hors /data/stacks → non managé (externe)."""
    _override_managed_stack(tmp_path, "MyApp")
    labels = {
        "com.docker.compose.project": "myproject",
        "com.docker.compose.project.working_dir": "/opt/elsewhere",
        "com.docker.compose.project.config_files": "/opt/elsewhere/docker-compose.yml",
    }
    assert dm._resolve_managed_stack(labels) is None


def test_resolve_managed_stack_no_labels(tmp_path):
    """Sans labels compose → None (inchangé)."""
    _override_managed_stack(tmp_path, "MyApp")
    assert dm._resolve_managed_stack({}) is None
    assert dm._resolve_managed_stack(None) is None


def test_full_spec_managed_true_by_location_override_name(tmp_path, monkeypatch):
    """Label surchargé + working_dir dans stacks → managed True, stack = MyApp."""
    _override_managed_stack(tmp_path, "MyApp")
    wd = str(Path(tmp_path) / "stacks" / "MyApp")
    fake = FakeDockerClient(containers=[_override_stack_container(working_dir=wd, config_files=wd + "/docker-compose.yml")])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)

    spec = dm._get_container_full_spec("abc1")
    assert spec is not None
    assert spec["managed"] is True
    assert spec["stack"] == "MyApp"


async def test_update_container_managed_by_location_override_name(tmp_path, monkeypatch):
    """Label surchargé + working_dir dans stacks → édition fonctionnelle dans le
    dossier RÉEL (aucun second dossier créé)."""
    stack_dir = _override_managed_stack(tmp_path, "MyApp")
    wd = str(Path(tmp_path) / "stacks" / "MyApp")
    fake = FakeDockerClient(containers=[_override_stack_container(working_dir=wd, config_files=wd + "/docker-compose.yml")])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)

    git_calls = []
    monkeypatch.setattr(dm, "_git_save", lambda stack, msg=None: git_calls.append((stack, msg)))
    monkeypatch.setattr(dm, "compose_up", lambda stack: __import__("asyncio").sleep(0) or {"success": True})

    result = await dm.update_container("abc1", _edit_spec())

    assert result["success"] is True
    # Écrit dans le dossier d'origine (casse réelle), pas un nouveau dossier.
    assert "nginx:1.25" in (stack_dir / "docker-compose.yml").read_text(encoding="utf-8")
    assert not (Path(tmp_path) / "stacks" / "myproject").exists()
    # git save sur le nom réel.
    assert git_calls and git_calls[0][0] == "MyApp"


async def test_update_container_external_working_dir_outside_refused(tmp_path, monkeypatch):
    """working_dir hors /data/stacks → refusé avec message enrichi."""
    _override_managed_stack(tmp_path, "MyApp")
    fake = FakeDockerClient(containers=[_override_stack_container(working_dir="/opt/elsewhere", config_files="/opt/elsewhere/docker-compose.yml")])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)

    result = await dm.update_container("abc1", _edit_spec())
    assert result["success"] is False
    assert result["error"] == dm.EXTERNAL_STACK_EDIT_REFUSED
    assert "déployez-la depuis Docky" in result["error"]


async def test_update_container_no_compose_labels_unchanged(tmp_path, monkeypatch):
    """Sans labels compose → chemin standalone inchangé (pas de refus, pas
    d'édition de compose)."""
    _override_managed_stack(tmp_path, "MyApp")
    attrs = {"Config": {"Image": "nginx:latest", "Labels": {}}}
    fake = FakeDockerClient(containers=[FakeContainer(container_id="solo1", name="web", attrs=attrs)])
    monkeypatch.setattr(dm, "get_docker_client", lambda: fake)

    recreated = {}
    async def fake_recreate(c, container_id, spec, client, attrs):
        recreated["called"] = True
        return {"success": True, "output": "recreated"}
    monkeypatch.setattr(dm, "_recreate_container", fake_recreate)

    result = await dm.update_container("solo1", _edit_spec())
    assert result["success"] is True
    assert recreated.get("called") is True
    # Aucun dossier surchargé créé, aucun compose édité.
    assert not (Path(tmp_path) / "stacks" / "myproject").exists()
