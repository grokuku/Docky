"""Historique git des stacks (extrait de docker_manager).

Init/commit/historique/restauration dans le dépôt git de ``data/stacks``, et
gestion du paramètre de rétention. Aucune dépendance vers docker_manager :
tous les symboles sont ré-exportés dans le namespace ``agent.docker_manager``
(façade). ``_git_init`` / ``_git_save`` sont monkeypatchés par les tests via
``agent.docker_manager`` ; l'appelant (import_stack) les résout donc via ``_dm()``.
"""

import logging
import subprocess
from pathlib import Path

from agent.config import get_data_dir

logger = logging.getLogger(__name__)


def _dm():
    """Résolution tardive du namespace agent.docker_manager (évite tout cycle)."""
    from agent import docker_manager
    return docker_manager


def _git_init() -> None:
    """Initialize git repo in stacks directory if not exists."""
    stacks_dir = Path(get_data_dir()) / 'stacks'
    git_dir = stacks_dir / '.git'
    if not git_dir.exists():
        subprocess.run(["git", "init"], cwd=str(stacks_dir), capture_output=True)
        subprocess.run(["git", "config", "user.name", "Docky"], cwd=str(stacks_dir), capture_output=True)
        subprocess.run(["git", "config", "user.email", "docky@local"], cwd=str(stacks_dir), capture_output=True)
        # .gitignore to exclude .git itself and sensitive files
        with open(stacks_dir / '.gitignore', 'w') as f:
            f.write(".git\n")
        logger.info("Git repository initialized in %s", stacks_dir)


def _git_save(stack_name: str, message: str = None) -> None:
    """Auto-commit the current state of a stack's files."""
    stacks_dir = Path(get_data_dir()) / 'stacks'
    _git_init()

    stack_path = stacks_dir / stack_name
    if not stack_path.exists():
        return

    # Add all files in the stack directory
    add = subprocess.run(["git", "add", str(stack_path)], cwd=str(stacks_dir), capture_output=True, text=True)
    if add.returncode != 0:
        logger.warning("git add failed for stack '%s': %s", stack_name, add.stderr.strip())

    # Commit
    from datetime import datetime
    msg = message or f"Save {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    commit = subprocess.run(["git", "commit", "-m", msg], cwd=str(stacks_dir), capture_output=True, text=True)
    if commit.returncode != 0:
        # Nothing staged (no change) is expected and harmless; anything else
        # should be visible in the logs instead of failing silently.
        if "nothing to commit" not in commit.stderr:
            logger.warning("git commit failed for stack '%s': %s", stack_name, commit.stderr.strip())

    # Appliquer la rétention (max_versions)
    try:
        settings = get_history_settings()
        max_versions = settings.get('max_versions', 50)
        _git_cleanup(stack_name, max_versions)
    except Exception as e:
        logger.warning("History cleanup failed for stack '%s': %s", stack_name, e)


def _get_git_history(stack_name: str = None, max_count: int = 50) -> list:
    """Return git log for a stack (or all stacks if None)."""
    stacks_dir = Path(get_data_dir()) / 'stacks'
    git_dir = stacks_dir / '.git'
    if not git_dir.exists():
        return []

    path_filter = [str(stacks_dir / stack_name)] if stack_name else []
    cmd = ["git", "log", f"--max-count={max_count}", "--format=%H|%ct|%s", "--date=unix"]
    if path_filter:
        cmd += ["--", *path_filter]

    result = subprocess.run(cmd, cwd=str(stacks_dir), capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        return []

    history = []
    for line in result.stdout.strip().split('\n'):
        parts = line.split('|', 2)
        if len(parts) == 3:
            from datetime import datetime
            history.append({
                "hash": parts[0],
                "date": datetime.fromtimestamp(int(parts[1])).isoformat(),
                "message": parts[2],
            })
    return history


def _get_git_version(stack_name: str, hash: str) -> dict:
    """Return the content of a specific version for a stack."""
    stacks_dir = Path(get_data_dir()) / 'stacks'

    # Get the file content at that commit
    compose_path = f"{stack_name}/docker-compose.yml"
    result = subprocess.run(
        ["git", "show", f"{hash}:{compose_path}"],
        cwd=str(stacks_dir), capture_output=True, text=True
    )
    if result.returncode != 0:
        return None

    # Also get commit info
    log_result = subprocess.run(
        ["git", "log", "-1", "--format=%H|%ct|%s", hash],
        cwd=str(stacks_dir), capture_output=True, text=True
    )

    info = {"hash": hash, "content": result.stdout}
    if log_result.returncode == 0 and log_result.stdout.strip():
        parts = log_result.stdout.strip().split('|', 2)
        if len(parts) == 3:
            from datetime import datetime
            info["date"] = datetime.fromtimestamp(int(parts[1])).isoformat()
            info["message"] = parts[2]

    return info


def _git_restore(stack_name: str, hash: str) -> dict:
    """Restore a stack's file to a specific version."""
    stacks_dir = Path(get_data_dir()) / 'stacks'

    # Restore the file
    result = subprocess.run(
        ["git", "checkout", hash, "--", str(stacks_dir / stack_name)],
        cwd=str(stacks_dir), capture_output=True, text=True
    )
    if result.returncode != 0:
        return {"success": False, "error": result.stderr}

    # Auto-commit the restore
    _git_save(stack_name, f"Restauré depuis {hash[:8]}")

    return {"success": True, "output": f"Stack {stack_name} restaurée vers {hash[:8]}"}


def _git_cleanup(stack_name: str, max_versions: int = 50) -> None:
    """Keep only the latest ``max_versions`` snapshots of a stack, squash older ones.

    The repository in ``data/stacks`` is shared by every stack, so commits
    touching one stack are interleaved with commits for other stacks. To honour
    the setting ("keep the N newest backups, compress the older ones") the
    squash is done with a soft reset plus per-version replays:

    1. soft-reset HEAD to the newest commit that must be folded away — the index
       and working tree keep the current (newest) state, so no data is lost;
    2. build a synthetic ``Historique antérieur compressé`` baseline commit that
       holds the stack files exactly as they were just before the oldest kept
       snapshot (all other files stay at their newest state);
    3. replay the kept snapshots oldest → newest, checking out each version's
       stack files and committing with the original message. The final tree is
       identical to the previous HEAD.

    Any failure leaves the working tree intact and only logs a warning.
    """
    stacks_dir = Path(get_data_dir()) / 'stacks'
    git_dir = stacks_dir / '.git'
    if not git_dir.exists():
        return

    stack_path = str(stacks_dir / stack_name)

    def _run(args):
        return subprocess.run(args, cwd=str(stacks_dir), capture_output=True, text=True)

    # Commits touching this stack, newest first.
    log = _run(["git", "log", "--format=%H", "--", stack_path])
    if log.returncode != 0:
        return
    commits = [ln for ln in log.stdout.strip().split('\n') if ln]
    if len(commits) <= max_versions:
        return  # Nothing to clean up

    try:
        kept = commits[:max_versions]                # newest snapshots to keep
        oldest_kept = kept[-1]
        squash_until = commits[max_versions]         # newest snapshot to fold away

        # Snapshot the tree we must end up with (current HEAD = newest state).
        head_tree = _run(["git", "rev-parse", "HEAD^{tree}"]).stdout.strip()

        # 1. Soft-reset to the newest squashed commit: only the branch pointer
        #    moves, the index and working tree keep the newest state.
        if _run(["git", "reset", "--soft", squash_until]).returncode != 0:
            logger.warning("Cleanup failed for stack '%s': git reset failed", stack_name)
            return

        # 2. Restore the stack files to their state just before ``oldest_kept``
        #    and create the compressed baseline as a fresh root commit. Older
        #    history (for this and other stacks) is collapsed into it while all
        #    file contents stay at their newest state.
        if _run(["git", "checkout", squash_until, "--", stack_path]).returncode != 0:
            logger.warning("Cleanup failed for stack '%s': git checkout failed", stack_name)
            return
        tree = _run(["git", "write-tree"]).stdout.strip()
        baseline = _run(["git", "commit-tree", tree, "-m", "Historique antérieur compressé"]).stdout.strip()
        if not tree or not baseline:
            logger.warning("Cleanup failed for stack '%s': could not build baseline", stack_name)
            return
        if _run(["git", "reset", "--soft", baseline]).returncode != 0:
            return

        # 3. Replay the kept versions oldest → newest with their original
        #    messages, so recent backups stay individually browsable.
        for rev in reversed(kept):
            if _run(["git", "checkout", rev, "--", stack_path]).returncode != 0:
                break
            msg = _run(["git", "log", "-1", "--format=%s", rev]).stdout.strip()
            _run(["git", "commit", "-m", msg, "--allow-empty"])

        # Sanity check: the rewritten history must hold the exact same files.
        final_tree = _run(["git", "rev-parse", "HEAD^{tree}"]).stdout.strip()
        if final_tree != head_tree:
            logger.warning(
                "Cleanup for stack '%s' produced a different tree (%s != %s)!",
                stack_name, final_tree[:12], head_tree[:12],
            )
        logger.info("Cleaned up history for stack '%s': kept %d versions", stack_name, max_versions)
    except Exception as e:
        logger.warning("Failed to cleanup history for '%s': %s", stack_name, e)


def get_history_settings() -> dict:
    """Get history retention settings from settings.yaml.

    Falls back to a sane default (``max_versions=50``) when the file is
    missing or malformed so that git cleanup never breaks on bad config.
    """
    import yaml
    try:
        settings_path = Path(get_data_dir()) / 'settings.yaml'
        if settings_path.exists():
            with open(settings_path) as f:
                settings = yaml.safe_load(f) or {}
            if isinstance(settings, dict):
                retention = settings.get('history_retention') or {}
                max_versions = retention.get('max_versions')
                if isinstance(max_versions, int) and max_versions > 0:
                    return {'max_versions': max_versions}
    except Exception as e:
        logger.warning("Could not read history retention settings: %s", e)
    return {'max_versions': 50}


def set_history_settings(max_versions: int) -> None:
    """Save history retention settings."""
    import yaml
    settings_path = Path(get_data_dir()) / 'settings.yaml'
    settings = {}
    if settings_path.exists():
        with open(settings_path) as f:
            settings = yaml.safe_load(f) or {}
    settings['history_retention'] = {'max_versions': max_versions}
    with open(settings_path, 'w') as f:
        yaml.dump(settings, f)

