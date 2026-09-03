"""Création de stack : l'encodage UTF-8 doit être garanti partout.

Reproduit le bug : ``'ascii' codec can't encode character '\xf9' (ù)`` levé à
la création d'une stack nommée ``ai-swarm`` (ASCII) quand le payload du compose
porte des métadonnées non-ASCII (ex. ``# @description: description avec ù``).

Le message de commit git passé par ``create_stack`` -> ``_git_save`` est encodé
par ``os.fsencode`` (encodage du système de fichiers), **ASCII sur les hôtes
C/POSIX hérités** : un message non-ASCII lève alors ``UnicodeEncodeError``.
Le correctif passe le message de commit en **bytes UTF-8** (contourne fsencode).
"""

import subprocess
from pathlib import Path

from agent import docker_manager as dm
from agent.docker import git_history as gh


def _ascii_fsencode_subprocess(captured):
    """Rejoue le comportement d'un hôte hérité où l'encodage du système de
    fichiers est ASCII : tout argument ``str`` non-ASCII leverait une
    ``UnicodeEncodeError`` ; un argument ``bytes`` est transmis tel quel."""
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
        captured.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
    return run


def test_create_stack_non_ascii_description_writes_utf8_and_commits_bytes(tmp_path, monkeypatch):
    """create_stack ne doit plus lever d'erreur d'encodage ASCII quand le compose
    porte une description non-ASCII, et doit écrire le fichier en UTF-8."""
    monkeypatch.setenv("DOCKY_DATA_DIR", str(tmp_path))
    stacks_dir = Path(dm.get_data_dir()) / "stacks"
    stacks_dir.mkdir(parents=True)

    name = "ai-swarm"  # nom de stack ASCII
    compose = (
        "# @name: ai-swarm\n"
        "# @category: Web\n"
        "# @description: description avec ù\n"
        "services:\n"
        "  app:\n"
        "    image: nginx:latest\n"
    )

    captured = []
    monkeypatch.setattr(gh, "_git_init", lambda: None)
    monkeypatch.setattr(gh.subprocess, "run", _ascii_fsencode_subprocess(captured))

    # Ne lève plus UnicodeEncodeError (le bug d'origine).
    result = dm.create_stack(name, compose)

    # Le docker-compose.yml est bien écrit en UTF-8 (ù préservé).
    written = (stacks_dir / name / "docker-compose.yml").read_text(encoding="utf-8")
    assert "description avec ù" in written

    # Le message de commit git est passé en bytes UTF-8 -> contourne le
    # fsencode ASCII de l'hôte hérité.
    commit_argv = [a for a in captured if a[0] == "git" and "-m" in a]
    message_args = [a[a.index("-m") + 1] for a in commit_argv]
    assert message_args, "un commit git devait être émis pour la création"
    for m in message_args:
        assert isinstance(m, bytes), f"message de commit non-bytes: {m!r}"
        assert m.decode("utf-8") == f"Création de {name}"

    assert result["name"] == name
