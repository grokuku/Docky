"""``_resolve_compose_args`` : tous les args/cwd doivent être en bytes UTF-8.

Reproduit le cas LIVE : un nom de stack (et un chemin de data dir) contenant
``ù`` sous locale C/POSIX (``LC_ALL=C PYTHONCOERCECLOCALE=0 PYTHONUTF8=0``)
où l'encodage du système de fichiers (``os.fsencode``) est **ASCII**. Un
``str`` non-ASCII passé tel quel à ``subprocess.run`` /
``create_subprocess_exec`` dans ``_resolve_compose_args`` lèverait alors
``UnicodeEncodeError: 'ascii' codec can't encode character '\xf9'``.

Le correctif encode chaque élément d'argv et le working directory en bytes
UTF-8 dans ``_resolve_compose_args`` (le ``_b()`` ne touchait jusqu'ici que
``git_history.py``), ce qui contourne le fsencode ASCII de l'hôte.
"""

import subprocess
from pathlib import Path

from agent import docker_manager as dm


def _ascii_fsencode_subprocess(captured):
    """Simule un hôte C/POSIX : un arg ``str`` non-ASCII lèverait
    ``UnicodeEncodeError`` ; un arg ``bytes`` est transmis tel quel."""
    def run(args, **kwargs):
        for item in args:
            if isinstance(item, str):
                try:
                    item.encode("ascii")
                except UnicodeEncodeError:
                    raise UnicodeEncodeError(
                        "ascii", item, 0, len(item),
                        f"'ascii' codec can't encode character in position: {item!r}",
                    )
        captured.append((args, kwargs.get("cwd")))
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
    return run


def test_resolve_compose_args_external_project_name_is_bytes(tmp_path, monkeypatch):
    """Cas LIVE : stack externe sans compose résolvable, nom de stack avec ``ù``.

    La branche ``--project-name`` reconstruit les args avec le ``stack_name``
    brut : c'est celle qui levait ``UnicodeEncodeError`` sur un nom de stack
    non-ASCII. Après le fix, tous les éléments d'argv doivent être des bytes
    UTF-8 et ``subprocess.run`` se construit sans erreur sur hôte à locale ASCII.

    (La valeur de ``DOCKY_DATA_DIR`` reste ASCII : sous locale C/POSIX un
    ``os.environ`` non-ASCII échouerait avant même notre code — le cas réel
    reproduit ici est le nom de la stack, pas l'env var.)
    """
    monkeypatch.setenv("DOCKY_DATA_DIR", str(tmp_path))
    (Path(dm.get_data_dir()) / "stacks").mkdir(parents=True)

    args, work_dir = dm._resolve_compose_args("stack-ù", "stop")

    for item in args:
        assert isinstance(item, bytes), f"arg non-bytes: {item!r}"
        item.decode("utf-8")  # doit être du UTF-8 valide
    assert b"stack-\xc3\xb9" in args  # 'ù' encodé en UTF-8 (bytes)
    assert not any(isinstance(a, str) for a in args)
    assert work_dir is None

    captured = []
    run = _ascii_fsencode_subprocess(captured)
    result = run(args, cwd=work_dir)
    assert result.returncode == 0
    assert captured[0][0] == args


def test_resolve_compose_args_managed_encodes_args_and_cwd(tmp_path, monkeypatch):
    """Stack gérée : ``args`` ET ``work_dir`` (passé en ``cwd``) en bytes UTF-8.

    Le chemin (data dir / nom de stack) est ASCII ici — sous locale C/POSIX on
    ne peut pas créer/monkeypatcher un chemin non-ASCII (os.fsencode ASCII
    l'interdit). L'objectif est de valider que le branche ``-f`` encode bien
    *tous* les éléments d'argv et le ``cwd`` en bytes, sans laisser un ``str``.
    """
    monkeypatch.setenv("DOCKY_DATA_DIR", str(tmp_path))
    stacks_dir = Path(dm.get_data_dir()) / "stacks"
    stack_dir = stacks_dir / "app"
    stack_dir.mkdir(parents=True)
    (stack_dir / "docker-compose.yml").write_text(
        "services:\n  app:\n    image: nginx:latest\n", encoding="utf-8"
    )

    args, work_dir = dm._resolve_compose_args("app", "up -d")

    for item in args:
        assert isinstance(item, bytes), f"arg non-bytes: {item!r}"
        item.decode("utf-8")
    assert not any(isinstance(a, str) for a in args)
    assert work_dir is not None
    assert isinstance(work_dir, bytes), f"cwd non-bytes: {work_dir!r}"
    assert b"app" in work_dir

    captured = []
    run = _ascii_fsencode_subprocess(captured)
    run(args, cwd=work_dir)
    assert captured[0][1] == work_dir
