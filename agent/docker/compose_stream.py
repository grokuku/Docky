"""Streaming ``docker compose`` (extrait de docker_manager).

Helpers d'exécution de sous-processus en streaming (sortie temps réel en
événements SSE), exécution compose non-streaming (``_run_compose``) et les
générateurs ``stream_*`` publics. Tous ces symboles sont ré-exportés dans le
namespace ``agent.docker_manager`` (façade).

Règle de la façade : les fonctions résidant encore dans docker_manager
(``_resolve_compose_args``, ``_resolve_stack_compose``, ``_invalidate_stack_update_cache``)
sont résolues au moment de l'appel via ``_dm()`` pour ne créer aucun cycle
d'import et rester indépendant des monkeypatchs.
"""

import asyncio
import logging
import os
import signal
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

logger = logging.getLogger(__name__)


def _dm():
    """Résolution tardive du namespace agent.docker_manager (évite tout cycle)."""
    from agent import docker_manager
    return docker_manager


# Idle timeout for streamed commands: if no output is produced for this many
# seconds the running process is killed. The counter only ticks during output
# silences — as long as lines keep arriving the command is allowed to run.
STREAM_IDLE_TIMEOUT = 120

# Event types emitted by the docker_manager streaming generators.
STREAM_EVENT_OUTPUT = "output"
STREAM_EVENT_RESULT = "result"


class StreamCommandError(Exception):
    """Raised when a streamed subprocess exits with a non-zero status.

    All output lines are yielded *before* this exception is raised, so the
    caller can display the full progress and then report the failure.
    """

    def __init__(self, message: str, returncode: Optional[int] = None, command: str = ""):
        super().__init__(message)
        self.message = message
        self.returncode = returncode
        self.command = command


async def _run_compose(stack_name: str, command: str, timeout: int = 300) -> Dict[str, Any]:
    """Run a docker compose subcommand for a stack (non-blocking).

    Works for both managed stacks (in /data/stacks/) and external stacks
    whose compose file path is derived from container labels.

    Uses :func:`asyncio.create_subprocess_exec` so the FastAPI event loop
    is not blocked while Docker pulls images or starts containers.

    This non-streaming variant accumulates stdout/stderr and returns a single
    result dict — kept for callers that do not need real-time output (e.g.
    spec updates that redeploy internally).
    """
    args, work_dir = _dm()._resolve_compose_args(stack_name, command)
    full_cmd = " ".join(args)
    try:
        if work_dir:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=work_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
        if proc.returncode == 0:
            # `docker compose` writes its progress / status messages to stderr
            # (not stdout). Merge stderr into the output on success so that
            # update / deploy / up commands return visible output instead of
            # an empty string.
            combined = stdout
            if stderr:
                combined = (combined + "\n" + stderr) if combined else stderr
            return {"success": True, "output": combined, "command": full_cmd}
        else:
            return {"success": False, "error": stderr or stdout, "command": full_cmd}
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return {"success": False, "error": "Command timed out", "command": full_cmd}
    except Exception as e:
        return {"success": False, "error": str(e), "command": full_cmd}


async def _run_command_stream(
    cmd: List[str],
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    idle_timeout: int = STREAM_IDLE_TIMEOUT,
) -> AsyncIterator[str]:
    """Yield stdout/stderr lines from a subprocess in real time.

    Both streams are read concurrently and each decoded line is yielded as
    soon as it is produced (docker compose writes its progress to stderr,
    so reading only stdout would defeat the purpose of streaming).

    **Idle timeout** — if *no* output arrives for ``idle_timeout`` seconds
    the process is SIGKILLed and :class:`asyncio.TimeoutError` is raised. As
    long as lines keep flowing the command is allowed to run indefinitely:
    the timeout only measures output silence, not total runtime.

    If the process exits with a non-zero status, all output lines are first
    yielded and then :class:`StreamCommandError` is raised.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # New session so a timeout kill can target the whole process group
        # (docker CLI may spawn children that would otherwise keep the pipes
        # open and delay reaping of the killed leader).
        start_new_session=True,
    )
    queue: asyncio.Queue = asyncio.Queue()
    remaining_readers = 2  # stdout + stderr

    async def _pump(stream):
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\n")
                if text:
                    await queue.put(text)
        except (asyncio.CancelledError, Exception):
            pass
        finally:
            # Sentinel: this reader is done (the consumer counts them).
            await queue.put(None)

    readers = [
        asyncio.ensure_future(_pump(proc.stdout)),
        asyncio.ensure_future(_pump(proc.stderr)),
    ]
    try:
        while remaining_readers > 0:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=idle_timeout)
            except asyncio.TimeoutError:
                raise TimeoutError(f"Timeout: no output for {idle_timeout}s") from None
            if item is None:
                remaining_readers -= 1
            else:
                yield item
    finally:
        for t in readers:
            t.cancel()
        if proc.returncode is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        # Bounded reaping: never hang the stream on orphaned children.
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass

    if proc.returncode != 0:
        raise StreamCommandError(
            f"Command failed with exit code {proc.returncode}",
            returncode=proc.returncode,
            command=" ".join(cmd),
        )


async def _stream_compose(
    stack_name: str,
    command: str,
    idle_timeout: int = STREAM_IDLE_TIMEOUT,
) -> AsyncIterator[Dict[str, Any]]:
    """Run a ``docker compose`` subcommand and yield stream events.

    Yields ``{"type": "output", "line": str}`` for each output line, then a
    final ``{"type": "result", "success": bool, "output": str, "error": str,
    "command": str}`` event.  On a timeout the process is killed and the
    result event reports ``success: False``.
    """
    args, work_dir = _dm()._resolve_compose_args(stack_name, command)
    full_cmd = " ".join(args)
    output_lines: list = []
    try:
        async for line in _run_command_stream(args, cwd=work_dir, idle_timeout=idle_timeout):
            output_lines.append(line)
            yield {"type": STREAM_EVENT_OUTPUT, "line": line}
    except asyncio.TimeoutError as e:
        yield {
            "type": STREAM_EVENT_RESULT,
            "success": False,
            "output": "\n".join(output_lines),
            "error": str(e),
            "command": full_cmd,
        }
        return
    except StreamCommandError as e:
        yield {
            "type": STREAM_EVENT_RESULT,
            "success": False,
            "output": "\n".join(output_lines),
            "error": e.message or f"Command failed with exit code {e.returncode}",
            "command": full_cmd,
        }
        return
    except Exception as e:
        yield {
            "type": STREAM_EVENT_RESULT,
            "success": False,
            "output": "\n".join(output_lines),
            "error": str(e),
            "command": full_cmd,
        }
        return
    yield {
        "type": STREAM_EVENT_RESULT,
        "success": True,
        "output": "\n".join(output_lines),
        "error": "",
        "command": full_cmd,
    }


async def _stream_compose_step(
    name: str,
    command: str,
    label: Optional[str] = None,
    idle_timeout: int = STREAM_IDLE_TIMEOUT,
) -> AsyncIterator[Dict[str, Any]]:
    """Run one compose command, optionally prefixing it with a label line."""
    if label:
        yield {"type": STREAM_EVENT_OUTPUT, "line": label}
    async for evt in _stream_compose(name, command, idle_timeout=idle_timeout):
        yield evt


async def _stream_command_step(
    args: List[str],
    cwd: Optional[str],
    label: Optional[str],
    idle_timeout: int,
    collect: List[str],
) -> AsyncIterator[Dict[str, Any]]:
    """Run one CLI command and stream its output as SSE events.

    Yields ``output`` events (a labeled header plus every line, all also
    appended to *collect* so the caller can build the final summary) and then
    a single ``result`` event that the caller inspects to decide the flow.

    Used by the single-container update sequence (``pull`` / ``stop`` /
    ``rm`` / ``compose up`` steps) where each step must be streamed and
    labelled while the overall flow stays under the caller's control.
    """
    if label:
        collect.append(label)
        yield {"type": STREAM_EVENT_OUTPUT, "line": label}
    full_cmd = " ".join(args)
    try:
        async for line in _run_command_stream(args, cwd=cwd, idle_timeout=idle_timeout):
            collect.append(line)
            yield {"type": STREAM_EVENT_OUTPUT, "line": line}
    except StreamCommandError as e:
        yield {
            "type": STREAM_EVENT_RESULT,
            "success": False,
            "output": "\n".join(collect),
            "error": e.message or f"Command failed with exit code {e.returncode}",
            "command": full_cmd,
        }
        return
    except asyncio.TimeoutError as e:
        yield {
            "type": STREAM_EVENT_RESULT,
            "success": False,
            "output": "\n".join(collect),
            "error": str(e),
            "command": full_cmd,
        }
        return
    except Exception as e:
        yield {
            "type": STREAM_EVENT_RESULT,
            "success": False,
            "output": "\n".join(collect),
            "error": str(e),
            "command": full_cmd,
        }
        return
    yield {
        "type": STREAM_EVENT_RESULT,
        "success": True,
        "output": "\n".join(collect),
        "error": "",
        "command": full_cmd,
    }


def _compose_up_command(name: str) -> str:
    """Return the compose subcommand used to bring a stack up.

    ``--remove-orphans`` garantit qu'un service retiré du compose (son
    container n'étant plus défini dans le fichier) est supprimé au prochain
    ``up`` / déploiement au lieu de rester en vie comme container orphelin.

    External stacks without a compose file fall back to ``start`` (which
    operates on the existing containers via ``--project-name``).
    """
    compose_file, _cwd = _dm()._resolve_stack_compose(name)
    if compose_file is None or not Path(compose_file).exists():
        return "start"
    return "up -d --remove-orphans"


def _compose_down_command(name: str) -> str:
    """Return the compose subcommand used to take a stack down."""
    compose_file, _cwd = _dm()._resolve_stack_compose(name)
    if compose_file is None or not Path(compose_file).exists():
        return "stop"
    return "down"


async def stream_start_stack(name: str, idle_timeout: int = STREAM_IDLE_TIMEOUT) -> AsyncIterator[Dict[str, Any]]:
    """Stream ``docker compose up -d --remove-orphans`` for a stack (the "Démarrer" action).

    ``up -d`` starts existing containers AND creates the missing ones (e.g.
    after a ``down`` or a first deploy), which is the intended semantics of a
    "start" button; ``--remove-orphans`` supprime les containers du projet qui
    ne sont plus dans le compose. External stacks without a compose file fall
    back to ``docker compose start`` (via :func:`_compose_up_command`).
    """
    async for evt in _stream_compose_step(name, _compose_up_command(name), label="── docker compose up -d --remove-orphans ──", idle_timeout=idle_timeout):
        yield evt


async def stream_stop_stack(name: str, idle_timeout: int = STREAM_IDLE_TIMEOUT) -> AsyncIterator[Dict[str, Any]]:
    """Stream ``docker compose stop`` for a stack."""
    async for evt in _stream_compose_step(name, "stop", label="── docker compose stop ──", idle_timeout=idle_timeout):
        yield evt


async def stream_restart_stack(name: str, idle_timeout: int = STREAM_IDLE_TIMEOUT) -> AsyncIterator[Dict[str, Any]]:
    """Stream ``docker compose restart`` for a stack."""
    async for evt in _stream_compose_step(name, "restart", label="── docker compose restart ──", idle_timeout=idle_timeout):
        yield evt


async def stream_update_stack(name: str, idle_timeout: int = STREAM_IDLE_TIMEOUT) -> AsyncIterator[Dict[str, Any]]:
    """Stream ``docker compose pull`` then ``docker compose up -d`` for a stack.

    ``up -d --remove-orphans`` supprime les containers devenus orphelins (un
    service retiré du compose est donc supprimé au prochain update).

    Raises :class:`FileNotFoundError` if the stack directory does not exist.
    """
    compose_file, _cwd = _dm()._resolve_stack_compose(name)
    if compose_file is None or not Path(compose_file).exists():
        raise FileNotFoundError(f"Stack '{name}' not found")
    # Step 1: pull (failure is fatal — no point trying to bring the stack up)
    async for evt in _stream_compose_step(name, "pull", label="── docker compose pull ──", idle_timeout=idle_timeout):
        if evt.get("type") == STREAM_EVENT_RESULT:
            if not evt.get("success"):
                yield evt
                return
            # pull succeeded: swallow the intermediate result event
        else:
            yield evt
    # Step 2: up -d (--remove-orphans: un service retiré du compose est supprimé)
    up_result = None
    async for evt in _stream_compose_step(name, "up -d --remove-orphans", label="── docker compose up -d --remove-orphans ──", idle_timeout=idle_timeout):
        if evt.get("type") == STREAM_EVENT_RESULT:
            up_result = evt
        yield evt
    # Pull + up -d réussis : les digests locaux viennent de changer, forcer un
    # check frais (le cache TTL 300 s pourrait encore renvoyer les digests
    # distants pré-pull et laisser le badge "update dispo" visible).
    if up_result is not None and up_result.get("success"):
        _dm()._invalidate_stack_update_cache(name)


async def stream_deploy_stack(name: str, idle_timeout: int = STREAM_IDLE_TIMEOUT) -> AsyncIterator[Dict[str, Any]]:
    """Stream ``docker compose down`` then ``docker compose up -d`` for a stack.

    ``down`` (sans ``-v`` : les volumes nommés sont conservés) puis
    ``up -d --remove-orphans`` : un service retiré du compose est donc
    supprimé au prochain déploiement.

    Raises :class:`FileNotFoundError` if the stack directory does not exist.
    """
    compose_file, _cwd = _dm()._resolve_stack_compose(name)
    if compose_file is None or not Path(compose_file).exists():
        raise FileNotFoundError(f"Stack '{name}' not found")
    # Step 1: down (a failing down is fatal)
    async for evt in _stream_compose_step(name, _compose_down_command(name), label="── docker compose down ──", idle_timeout=idle_timeout):
        if evt.get("type") == STREAM_EVENT_RESULT:
            if not evt.get("success"):
                yield evt
                return
        else:
            yield evt
    # Step 2: up -d (--remove-orphans: un service retiré du compose est supprimé)
    up_result = None
    async for evt in _stream_compose_step(name, _compose_up_command(name), label="── docker compose up -d --remove-orphans ──", idle_timeout=idle_timeout):
        if evt.get("type") == STREAM_EVENT_RESULT:
            up_result = evt
        yield evt
    # Déploy réussi : les containers viennent d'être recréés à partir des
    # images locales. Si une de ces images a changé entre-temps (pull manuel,
    # update d'un container), le badge peut être faux ; invalider est trivial
    # et sans risque (le prochain check relit simplement le registre).
    if up_result is not None and up_result.get("success"):
        _dm()._invalidate_stack_update_cache(name)

