"""Docker SDK client utilities for the Docky Agent service.

Adapted from ``app/docker_manager/client.py`` — all Docker SDK functions
needed by the agent: container management, stack management, file editing,
ports scanning and update checks.
"""

import asyncio
import logging
import os
import shutil
import struct
import subprocess
import time
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

import docker
from docker.errors import DockerException, NotFound, APIError

from agent.config import get_data_dir

# Modules cohésifs extraits de ce fichier (façade : ré-export dans le namespace
# agent.docker_manager pour préserver routes.py / main.py et les monkeypatchs
# des tests qui ciblent agent.docker_manager.<symbole>).
from agent.docker.validation import (
    get_stacks_dir,
    safe_join,
    validate_filename,
    validate_stack_name,
    _stack_dir,
)

logger = logging.getLogger(__name__)

# Pseudo-stack name used to group containers that are not part of any
# Docker Compose project (i.e. standalone containers).
STANDALONE_STACK_NAME = "Standalone"

# ---------------------------------------------------------------------------
# Streaming command execution
# ---------------------------------------------------------------------------
# Constantes + helpers de streaming déplacés vers agent/docker/compose_stream.py
# et ré-exportés ici (façade) pour préserver routes.py/main.py et les
# monkeypatchs des tests ciblant agent.docker_manager.*.
from agent.docker.compose_stream import (
    STREAM_EVENT_OUTPUT,
    STREAM_EVENT_RESULT,
    STREAM_IDLE_TIMEOUT,
    StreamCommandError,
    _compose_down_command,
    _compose_up_command,
    _run_command_stream,
    _run_compose,
    _stream_command_step,
    _stream_compose,
    _stream_compose_step,
    stream_deploy_stack,
    stream_restart_stack,
    stream_start_stack,
    stream_stop_stack,
    stream_update_stack,
)

# Git history + import (extraits vers agent/docker/git_history.py et
# agent/docker/import_stack.py, ré-exportés ici pour préserver routes.py/main.py
# et les monkeypatchs des tests ciblant agent.docker_manager.*).
from agent.docker.git_history import (
    _get_git_history,
    _get_git_version,
    _git_cleanup,
    _git_init,
    _git_restore,
    _git_save,
    get_history_settings,
    set_history_settings,
)
from agent.docker.import_stack import import_stack

# Ports + events (extraits vers agent/docker/ports.py et agent/docker/events.py,
# ré-exportés ici pour préserver routes.py/main.py et les monkeypatchs des tests
# ciblant agent.docker_manager.*).
from agent.docker.ports import (
    _parse_netstat_output,
    _parse_proc_net,
    _parse_ss_output,
    _scan_system_ports,
    get_used_ports,
)
from agent.docker.events import watch_docker_events


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


def get_docker_client() -> docker.DockerClient:
    """Return a Docker SDK client.

    Tries an explicit unix socket first, then falls back to
    ``docker.from_env()`` so the environment (e.g. DOCKER_HOST) is
    respected.
    """
    socket_path = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
    try:
        if os.path.exists(socket_path):
            return docker.DockerClient(base_url=f"unix://{socket_path}")
    except DockerException:
        pass
    return docker.from_env()


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------

def _container_to_dict(c, managed_stacks: Optional[set] = None) -> Dict[str, Any]:
    """Convert a Docker container object to a serialisable dict.

    If *managed_stacks* is provided (a set of stack directory names), the
    ``stack`` field is normalised to match the original case of the stack
    name as stored on the filesystem.  This prevents case-mismatch bugs
    between Docker labels (always lowercased) and directory names (original
    case).
    """
    labels = c.attrs.get("Config", {}).get("Labels", {}) or {}
    state = c.attrs.get("State", {})
    ports_raw = c.ports or {}
    port_list: List[Dict[str, Any]] = []
    if isinstance(ports_raw, dict):
        for container_port, bindings in ports_raw.items():
            entry: Dict[str, Any] = {"container": container_port}
            if bindings:
                for b in bindings:
                    port_list.append({
                        "container": container_port,
                        "host_ip": b.get("HostIp", "0.0.0.0") if isinstance(b, dict) else "",
                        "host_port": b.get("HostPort", "") if isinstance(b, dict) else str(b),
                    })
            else:
                port_list.append(entry)

    status_label = c.status
    health = state.get("Health", {}).get("Status") if isinstance(state.get("Health"), dict) else None

    # Extract the project name from Docker labels (always lowercased by Docker)
    project = labels.get("com.docker.compose.project") or None

    # Normalise to original case if it matches a managed stack
    if project is not None and managed_stacks:
        project_lower = project.lower()
        for ms in managed_stacks:
            if ms.lower() == project_lower:
                project = ms
                break

    return {
        "id": c.short_id,
        "name": c.name.lstrip("/") if c.name else "",
        "image": c.image.tags[0] if c.image.tags else str(c.image.id),
        "image_id": c.image.id,
        "status": status_label,
        "state": status_label,
        "health": health,
        "ports": port_list,
        "stack": project,
        "service": labels.get("com.docker.compose.service", ""),
        "managed": False,  # filled in by list_containers()
        "labels": labels,
        "created": c.attrs.get("Created", ""),
    }


def list_containers(all: bool = True) -> List[Dict[str, Any]]:
    """Return a list of containers with their key properties.

    Each container dict includes a ``managed`` boolean: ``True`` if the
    container belongs to a stack whose directory lives in ``/data/stacks/``
    (i.e. managed by Docky), ``False`` otherwise (external stack or
    standalone container).
    """
    try:
        client = get_docker_client()
        containers = client.containers.list(all=all)
    except DockerException as exc:
        logger.warning("list_containers failed: %s", exc)
        return []

    managed_names = _managed_stack_names()
    result: List[Dict[str, Any]] = []
    for c in containers:
        d = _container_to_dict(c, managed_stacks=managed_names)
        stack = d.get("stack", "")
        if stack is None:
            d["managed"] = True
        else:
            d["managed"] = stack in managed_names
        result.append(d)
    return result


def get_container(container_id: str) -> Optional[Dict[str, Any]]:
    """Return details for a single container, or ``None`` if not found."""
    try:
        client = get_docker_client()
        c = client.containers.get(container_id)
    except (NotFound, DockerException):
        return None
    managed_names = _managed_stack_names()
    return _container_to_dict(c, managed_stacks=managed_names)


def start_container(container_id: str) -> bool:
    """Start a container. Returns ``True`` on success."""
    try:
        client = get_docker_client()
        c = client.containers.get(container_id)
        c.start()
        return True
    except (NotFound, DockerException, APIError):
        return False


def stop_container(container_id: str) -> bool:
    """Stop a container. Returns ``True`` on success."""
    try:
        client = get_docker_client()
        c = client.containers.get(container_id)
        c.stop(timeout=10)
        return True
    except (NotFound, DockerException, APIError):
        return False


def restart_container(container_id: str) -> bool:
    """Restart a container. Returns ``True`` on success."""
    try:
        client = get_docker_client()
        c = client.containers.get(container_id)
        c.restart(timeout=10)
        return True
    except (NotFound, DockerException, APIError):
        return False


def _get_container_full_spec(container_id: str) -> Optional[Dict[str, Any]]:
    """Return the complete spec of a container for the edit modal.

    Extracts ports, volumes, env, networks, labels, restart_policy and
    stack information from ``docker inspect`` output.
    """
    try:
        client = get_docker_client()
        c = client.containers.get(container_id)
    except Exception as exc:
        logger.warning("_get_container_full_spec failed for container '%s': %s", container_id, exc)
        return None

    attrs = c.attrs

    # Ports (dédupliqués avec un set)
    seen_ports = set()
    ports = []
    for container_port, bindings in (attrs.get("NetworkSettings", {}).get("Ports", {}) or {}).items():
        if bindings:
            for b in bindings:
                key = (container_port, b.get("HostPort", ""))
                if key not in seen_ports:
                    seen_ports.add(key)
                    ports.append({"host_port": b.get("HostPort", ""), "container_port": container_port})
        else:
            key = (container_port, "")
            if key not in seen_ports:
                seen_ports.add(key)
                ports.append({"host_port": "", "container_port": container_port})

    # Volumes (mounts)
    volumes = []
    for m in attrs.get("Mounts", []):
        if m.get("Type") == "bind":
            volumes.append({
                "host_path": m.get("Source", ""),
                "container_path": m.get("Destination", ""),
                "mode": "ro" if "ro" in (m.get("Mode", "") or "") else "rw"
            })

    # Env
    raw_env = attrs.get("Config", {}).get("Env") or []
    env = []
    for e in raw_env:
        if "=" in e:
            k, v = e.split("=", 1)
            env.append({"key": k, "value": v})
        else:
            env.append({"key": e, "value": ""})

    # Networks
    networks = []
    for net_name, net_info in (attrs.get("NetworkSettings", {}).get("Networks", {}) or {}).items():
        networks.append({"name": net_name, "ip": net_info.get("IPAddress", "") or ""})

    # Labels
    raw_labels = attrs.get("Config", {}).get("Labels") or {}
    labels = [{"key": k, "value": v} for k, v in raw_labels.items()]

    # Restart policy
    restart_policy = attrs.get("HostConfig", {}).get("RestartPolicy", {}).get("Name", "no")

    # Stack (from compose labels)
    project = raw_labels.get("com.docker.compose.project", "")
    managed = bool(project and (get_data_dir() / "stacks" / project).exists())

    return {
        "name": c.name.lstrip("/"),
        "image": attrs.get("Config", {}).get("Image", ""),
        "status": c.status,
        "restart_policy": restart_policy or "no",
        "ports": ports,
        "volumes": volumes,
        "env": env,
        "networks": networks,
        "labels": labels,
        "stack": project,
        "managed": managed,
    }


def get_container_logs(container_id: str, tail: int = 100) -> List[Dict]:
    """Return the last ``tail`` log lines with timestamps and stream info.

    Returns a list of dicts:
        {"message": "2024-01-01T12:00:00.123456789Z log text", "stream": "stdout"|"stderr"}
    """
    try:
        client = get_docker_client()
        c = client.containers.get(container_id)
        is_tty = bool((c.attrs.get("Config", {}) or {}).get("Tty"))

        # Fetch the RAW bytes straight from the Docker daemon.
        #
        # IMPORTANT: we deliberately bypass ``client.api.logs()`` here.
        # Since docker-py 7, ``APIClient.logs()`` already demultiplexes the
        # stdout/stderr stream for non-TTY containers (it strips the 8-byte
        # frames inside ``_get_result_tty`` before returning the bytes), and
        # for TTY containers it returns the raw stream without any framing
        # either. Feeding that already-demultiplexed output to the frame
        # parser below would treat the log text itself as a frame header,
        # compute a bogus (huge) frame length and return no lines at all —
        # i.e. the persistent "no logs" / "Aucun log" bug.
        #
        # We therefore issue the raw HTTP GET ourselves so the multiplexed
        # 8-byte frames (when the container is not a TTY) are preserved and
        # we can recover the stream type (stdout/stderr).
        params = {
            "stdout": 1,
            "stderr": 1,
            "tail": tail,
            "timestamps": 1,
        }
        url = client.api._url("/containers/{0}/logs", container_id)
        raw = client.api._get(url, params=params, stream=False).content
    except (NotFound, DockerException, APIError):
        return []

    if not raw or not isinstance(raw, bytes):
        return []

    result: List[Dict] = []

    if is_tty:
        # TTY containers: the daemon returns a single raw stream with no
        # 8-byte multiplexed frames (stdout/stderr are merged by the TTY).
        for line in raw.decode("utf-8", errors="replace").splitlines():
            line = line.rstrip("\r")
            if line:
                result.append({"message": line, "stream": "stdout"})
        return result

    # Non-TTY containers: multiplexed frames with 8-byte headers.
    offset = 0
    while offset + 8 <= len(raw):
        stream_type = raw[offset]  # 1 = stdout, 2 = stderr
        # Bytes 1-3 are padding
        frame_len = struct.unpack_from(">I", raw, offset + 4)[0]
        offset += 8

        if offset + frame_len > len(raw):
            break

        frame_data = raw[offset : offset + frame_len]
        offset += frame_len

        try:
            msg = frame_data.decode("utf-8", errors="replace")
            # Strip trailing newlines (each frame may end with \n)
            msg = msg.rstrip("\r\n")
        except Exception:
            continue

        if not msg:
            continue

        result.append({
            "message": msg,
            "stream": "stdout" if stream_type == 1 else "stderr",
        })

    return result


def get_container_logs_stream(container_id: str, tail: int = 0):
    """Return a generator yielding log lines as they arrive (streaming)."""
    try:
        client = get_docker_client()
        c = client.containers.get(container_id)
        stream = c.logs(stdout=True, stderr=True, stream=True, follow=True, tail=tail)
        for chunk in stream:
            if isinstance(chunk, bytes):
                # Conserver le '\n' : le proxy WS le relaie tel quel et le
                # terminal a besoin des fins de ligne pour découper les frames
                # (rstrip("\n") cassait la détection de lignes côté client).
                yield chunk.decode("utf-8", errors="replace")
            else:
                yield str(chunk)
    except (NotFound, DockerException, APIError):
        return


def get_container_stats(container_id: str) -> Dict[str, Any]:
    """Return CPU and RAM stats for a container (one-shot snapshot)."""
    empty = {"cpu_percent": 0.0, "mem_usage": 0, "mem_limit": 0, "mem_percent": 0.0, "network_rx": 0, "network_tx": 0}
    try:
        client = get_docker_client()
        c = client.containers.get(container_id)
        stats = c.stats(stream=False)
    except (NotFound, DockerException, APIError):
        return empty

    cpu_delta = 0
    system_delta = 0
    cpu_count = 1
    cpu_percent = 0.0

    cpu_stats = stats.get("cpu_stats", {})
    precpu_stats = stats.get("precpu_stats", {})
    cpu_delta = cpu_stats.get("cpu_usage", {}).get("total_usage", 0) - precpu_stats.get("cpu_usage", {}).get("total_usage", 0)
    system_delta = cpu_stats.get("system_cpu_usage", 0) - precpu_stats.get("system_cpu_usage", 0)
    online_cpus = cpu_stats.get("online_cpus")
    if online_cpus:
        cpu_count = online_cpus
    else:
        per_cpu = cpu_stats.get("cpu_usage", {}).get("percpu_usage", [])
        cpu_count = len(per_cpu) if per_cpu else 1

    if system_delta > 0 and cpu_delta > 0:
        cpu_percent = (cpu_delta / system_delta) * cpu_count * 100.0

    mem_stats = stats.get("memory_stats", {})
    mem_usage = mem_stats.get("usage", 0)
    mem_limit = mem_stats.get("limit", 0)
    mem_percent = 0.0
    if mem_limit > 0:
        mem_percent = (mem_usage / mem_limit) * 100.0

    network_rx = 0
    network_tx = 0
    networks = stats.get("networks", {})
    if isinstance(networks, dict):
        for iface in networks.values():
            network_rx += iface.get("rx_bytes", 0)
            network_tx += iface.get("tx_bytes", 0)

    return {
        "cpu_percent": round(cpu_percent, 2),
        "mem_usage": mem_usage,
        "mem_limit": mem_limit,
        "mem_percent": round(mem_percent, 2),
        "network_rx": network_rx,
        "network_tx": network_tx,
    }


def exec_in_container(container_id: str, command: str, tty: bool = False) -> Dict[str, Any]:
    """Execute a command in a container and return output plus the exit code.

    Returns a dict with ``success`` (exit code 0), ``output`` (raw stdout/stderr
    text) and ``exit_code``. On a hard failure (container not found, docker
    daemon unreachable…) ``success`` is ``False`` and ``output`` starts with
    ``[error]``.
    """
    try:
        client = get_docker_client()
        c = client.containers.get(container_id)
        result = c.exec_run(command, tty=tty)
        output = result.output
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        exit_code = result.exit_code
        if exit_code is None:
            exit_code = -1
        return {
            "success": exit_code == 0,
            "output": output if isinstance(output, str) else str(output),
            "exit_code": exit_code,
        }
    except (NotFound, DockerException, APIError) as e:
        return {
            "success": False,
            "output": f"[error] {e}",
            "exit_code": -1,
        }


def exec_in_container_stream(container_id: str, command: str):
    """Execute a command in a container and yield output chunks (stream)."""
    try:
        client = get_docker_client()
        c = client.containers.get(container_id)
        result = c.exec_run(command, stream=True)
        for chunk in result.output:
            if isinstance(chunk, bytes):
                yield chunk.decode("utf-8", errors="replace")
            else:
                yield str(chunk)
    except (NotFound, DockerException, APIError) as e:
        yield f"[error] {e}"


def exec_interactive_start(container_id: str, shell: str = "/bin/bash") -> tuple:
    """Create an interactive exec instance with PTY.

    Returns ``(sock, exec_id, raw_sock)`` where *sock* is the original object
    returned by Docker SDK (use ``sock.close()`` to clean up) and *raw_sock*
    is the underlying ``socket.socket`` made non-blocking for use with
    ``asyncio``.

    Docker SDK ``exec_start(..., socket=True)`` can return different types:
    - ``socket.socket`` for TCP connections
    - ``SocketIO`` (urllib3 wrapper) for Unix socket connections
    - ``HTTPResponse`` wrapper in some configurations

    ``asyncio``'s ``sock_recv`` / ``sock_sendall`` require a real
    ``socket.socket``, so we extract the raw socket from whatever wrapper
    Docker SDK gives us.
    """
    client = get_docker_client()

    # Create exec instance with TTY
    exec_id = client.api.exec_create(
        container_id,
        ["/bin/sh", "-c", f"TERM=xterm-256color exec {shell} -l"],
        tty=True,
        stdin=True,
        stdout=True,
        stderr=True,
    )['Id']

    # Start exec with socket mode
    sock = client.api.exec_start(exec_id, tty=True, socket=True)

    # --- Extract the raw socket.socket from whatever wrapper Docker SDK gave us ---
    raw_sock = sock

    # Case 1: SocketIO (urllib3) — wraps socket.socket in ._sock
    if hasattr(sock, '_sock') and hasattr(sock._sock, 'setblocking'):
        raw_sock = sock._sock
    # Case 2: urllib3 HTTPResponse — sock._fp.fp is the socket
    elif hasattr(sock, '_fp') and hasattr(sock._fp, 'fp'):
        fp = sock._fp.fp
        # fp can itself be a SocketIO or a raw socket
        if hasattr(fp, '_sock') and hasattr(fp._sock, 'setblocking'):
            raw_sock = fp._sock
        elif hasattr(fp, 'raw') and hasattr(fp.raw, 'setblocking'):
            raw_sock = fp.raw
        elif hasattr(fp, 'setblocking'):
            raw_sock = fp
    # Case 3: already a raw socket.socket
    elif hasattr(sock, 'setblocking'):
        raw_sock = sock
    # Case 4: fallback — try _fp.fp.raw or similar deep nesting
    elif hasattr(sock, '_fp') and hasattr(sock._fp, 'fp') and hasattr(sock._fp.fp, 'raw'):
        raw_sock = sock._fp.fp.raw

    # Make the raw socket non-blocking for asyncio
    if hasattr(raw_sock, 'setblocking'):
        raw_sock.setblocking(False)
    else:
        # Last resort: try settimeout(0) on the original object
        try:
            sock.settimeout(0)
        except Exception:
            logger.warning("Could not make exec socket non-blocking (type=%s)",
                           type(sock).__name__)

    logger.info("Interactive exec %s started in container %s (sock type=%s, raw type=%s)",
                exec_id[:12], container_id[:12], type(sock).__name__, type(raw_sock).__name__)
    return sock, exec_id, raw_sock


def exec_resize(container_id: str, exec_id: str, height: int, width: int):
    """Resize the TTY for an exec instance."""
    client = get_docker_client()
    client.api.exec_resize(exec_id, height=height, width=width)
    logger.debug("Exec %s resized to %dx%d", exec_id[:12], width, height)


# ---------------------------------------------------------------------------
# Stacks
# ---------------------------------------------------------------------------


def _managed_stack_names() -> set:
    """Return the set of stack names present in /data/stacks/."""
    stacks_dir = get_stacks_dir()
    names = set()
    if stacks_dir.exists():
        for entry in stacks_dir.iterdir():
            if entry.is_dir():
                names.add(entry.name)
    return names


def _external_compose_info(stack_name: str) -> tuple:
    """Derive ``(compose_file_path, working_dir)`` for an external stack from
    the Docker Compose labels of its containers.

    Returns ``(None, None)`` if no information could be found.
    """
    try:
        client = get_docker_client()
        containers = client.containers.list(all=True)
    except DockerException as exc:
        logger.warning("_external_compose_info failed for stack '%s': %s", stack_name, exc)
        return None, None

    for c in containers:
        labels = c.attrs.get("Config", {}).get("Labels", {}) or {}
        if labels.get("com.docker.compose.project") != stack_name:
            continue
        config_files = labels.get("com.docker.compose.project.config_files", "")
        working_dir = labels.get("com.docker.compose.project.working_dir", "")
        if config_files:
            first = config_files.split(",")[0].strip()
            compose_path = Path(first)
            cwd = working_dir or str(compose_path.parent)
            return compose_path, cwd
        if working_dir:
            wd = Path(working_dir)
            for name in ["docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"]:
                candidate = wd / name
                if candidate.exists():
                    return candidate, working_dir
    return None, None


def _resolve_stack_compose(stack_name: str) -> tuple:
    """Resolve ``(compose_file_path, working_dir)`` for a stack whether it is
    managed by Docky (in /data/stacks/) or external.

    Returns ``(None, None)`` when the stack cannot be located.
    """
    # 1. Managed by Docky
    managed_path = get_stacks_dir() / stack_name
    if managed_path.exists():
        compose_file = _compose_file_path(managed_path)
        if compose_file is not None:
            return compose_file, str(managed_path)
    # 2. External stack detected via container labels
    return _external_compose_info(stack_name)


def list_stacks() -> List[Dict[str, Any]]:
    """List all stacks visible to the agent.

    Three kinds of stacks are returned:
    * **managed** – directories present in ``/data/stacks/`` (``managed: True``)
    * **external** – Docker Compose projects detected through container labels
      but whose files are not in ``/data/stacks/`` (``managed: False``)
    * **Standalone** – a pseudo-stack grouping every container that does not
      belong to any Compose project (``managed: False, standalone: True``)
    """
    result: List[Dict[str, Any]] = []
    seen: set = set()

    # 1. Stacks managed by Docky (in /data/stacks/)
    stacks_dir = get_stacks_dir()
    if stacks_dir.exists():
        for entry in sorted(stacks_dir.iterdir()):
            if not entry.is_dir():
                continue
            compose_candidates = [
                entry / "docker-compose.yml",
                entry / "docker-compose.yaml",
                entry / "compose.yml",
                entry / "compose.yaml",
            ]
            has_compose = any(p.exists() for p in compose_candidates)
            has_env = (entry / ".env").exists()
            result.append(
                {
                    "name": entry.name,
                    "path": str(entry),
                    "has_compose": has_compose,
                    "has_env": has_env,
                    "managed": True,
                    "standalone": False,
                }
            )
            seen.add(entry.name.lower())

    # 2. External stacks detected via container labels
    try:
        client = get_docker_client()
        containers = client.containers.list(all=True)
    except DockerException as exc:
        logger.warning("Failed to detect external stacks: %s", exc)
        containers = []

    has_standalone = False
    for c in containers:
        labels = c.attrs.get("Config", {}).get("Labels", {}) or {}
        project = labels.get("com.docker.compose.project", "")
        if not project:
            has_standalone = True
            continue
        if project.lower() in seen:
            continue
        seen.add(project.lower())
        working_dir = labels.get("com.docker.compose.project.working_dir", "")
        config_files = labels.get("com.docker.compose.project.config_files", "")
        # Deduce a source path for one-click import: prefer the explicit
        # working_dir label, otherwise fall back to the parent directory of
        # the first declared compose file.
        source_path = working_dir
        if not source_path and config_files:
            first = config_files.split(",")[0].strip()
            if first:
                source_path = str(Path(first).parent)
        result.append(
            {
                "name": project,
                "path": working_dir,
                "has_compose": True,
                "has_env": False,
                "managed": False,
                "standalone": False,
                "source_path": source_path or "",
            }
        )

    # 3. Pseudo "Standalone" stack for containers without a Compose project
    if has_standalone:
        result.append(
            {
                "name": STANDALONE_STACK_NAME,
                "path": "",
                "has_compose": False,
                "has_env": False,
                "managed": False,
                "standalone": True,
            }
        )

    return result


def get_stack_containers(stack_name: str) -> List[Dict[str, Any]]:
    """Return all containers belonging to a compose stack.

    The special ``Standalone`` pseudo-stack returns every container that is
    not part of any Docker Compose project.
    """
    containers = list_containers(all=True)
    if stack_name == STANDALONE_STACK_NAME:
        return [c for c in containers if not c.get("stack")]
    return [c for c in containers if c.get("stack") == stack_name]


def get_stack_status(stack_name: str) -> str:
    """Return the global status of a stack: 'running', 'stopped', 'partial', 'empty'."""
    containers = get_stack_containers(stack_name)
    if not containers:
        return "empty"
    running = sum(1 for c in containers if c["status"] == "running")
    if running == len(containers):
        return "running"
    elif running == 0:
        return "stopped"
    else:
        return "partial"


def get_stack_ports(stack_name: str) -> List[str]:
    """Return a sorted list of host ports used by a stack's containers."""
    containers = get_stack_containers(stack_name)
    ports: set[str] = set()
    for c in containers:
        for p in c.get("ports", []):
            host_port = p.get("host_port", "")
            if host_port:
                ports.add(host_port)
    return sorted(ports, key=lambda x: int(x) if x.isdigit() else 0)


def _compose_file_path(stack_path: Path) -> Optional[Path]:
    """Return the path to the compose file for a stack, or ``None``."""
    for name in ["docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"]:
        candidate = stack_path / name
        if candidate.exists():
            return candidate
    return None


def _resolve_compose_args(stack_name: str, command: str):
    """Return ``(args, work_dir)`` for a ``docker compose`` invocation.

    For managed stacks (or external stacks whose compose file was found via
    container labels), the ``-f`` flag is used.  For external stacks whose
    compose file could not be located, the ``--project-name`` flag is used
    instead, which allows commands such as ``stop``, ``restart`` and
    ``start`` to operate on the existing containers without needing the
    compose file.
    """
    compose_file, cwd = _resolve_stack_compose(stack_name)
    cmd_parts = command.split()

    if compose_file is not None and Path(compose_file).exists():
        # Managed stack or external stack with a known compose file
        args = ["docker", "compose", "-f", str(compose_file)] + cmd_parts
        work_dir = cwd or str(Path(compose_file).parent)
    else:
        # External stack without a compose file: use --project-name
        args = ["docker", "compose", "--project-name", stack_name] + cmd_parts
        work_dir = None
    return args, work_dir


def _compose_project_services(project: str) -> Optional[Dict[str, Any]]:
    """Return the ``services`` mapping parsed from a project's compose file.

    Returns ``None`` when the compose file cannot be resolved or parsed (the
    caller then treats the service as unresolvable and falls back to a
    per-container update).  An empty dict means the file parsed but declares
    no services.
    """
    compose_file, _cwd = _resolve_stack_compose(project)
    if compose_file is None or not Path(compose_file).exists():
        return None
    try:
        import yaml
        compose = yaml.safe_load(compose_file.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("_compose_project_services failed to parse compose file '%s': %s", compose_file, exc)
        return None
    services = compose.get("services")
    if not isinstance(services, dict):
        return {}
    return services


def _compose_service_update_plan(project: str, service: str) -> Dict[str, Any]:
    """Return an update plan for a single compose service.

    Result keys:

    - ``compose_available`` — the project's compose file was resolved and
      parsed (false for external stacks whose compose file cannot be located).
    - ``service_declared`` — the service appears in the compose ``services:``
      mapping (false when the file exists but the service was removed).
    - ``build_only`` — the service is built locally (``build:``) with no
      ``image:`` key, so there is nothing to pull from a registry.
    - ``image`` — the service's configured ``image:`` value, if any.
    """
    services = _compose_project_services(project)
    if services is None:
        return {
            "compose_available": False,
            "service_declared": False,
            "build_only": False,
            "image": None,
        }
    svc = services.get(service)
    if not isinstance(svc, dict):
        return {
            "compose_available": True,
            "service_declared": False,
            "build_only": False,
            "image": None,
        }
    image = svc.get("image")
    build = svc.get("build") is not None
    return {
        "compose_available": True,
        "service_declared": True,
        "build_only": bool(build) and not (isinstance(image, str) and image.strip()),
        "image": image.strip() if isinstance(image, str) and image.strip() else None,
    }


async def compose_start(name: str) -> Dict[str, Any]:
    """Start existing containers for the given stack.

    Works with ``--project-name`` for external stacks that have no
    compose file available.
    """
    return await _run_compose(name, "start")


async def compose_up(name: str) -> Dict[str, Any]:
    """Run ``docker compose up -d --remove-orphans`` for the given stack.

    ``--remove-orphans`` supprime les containers du projet qui ne sont plus
    définis dans le compose (un service retiré du fichier est donc supprimé
    au prochain up / déploiement).

    For managed stacks (or external stacks with a known compose file),
    this runs ``docker compose up -d --remove-orphans``.  For external
    stacks whose compose file cannot be located, it falls back to ``docker
    compose --project-name {name} start`` which starts existing containers
    without needing the compose file.
    """
    compose_file, _cwd = _resolve_stack_compose(name)
    if compose_file is None or not Path(compose_file).exists():
        # External stack: use 'start' instead of 'up -d'
        return await _run_compose(name, "start")
    return await _run_compose(name, "up -d --remove-orphans")


async def compose_down(name: str) -> Dict[str, Any]:
    """Run ``docker compose down`` for the given stack.

    For external stacks without a compose file, falls back to ``stop``
    (via ``--project-name``) since ``down`` requires the compose file.
    """
    compose_file, _cwd = _resolve_stack_compose(name)
    if compose_file is None or not Path(compose_file).exists():
        # External stack: use 'stop' instead of 'down'
        return await _run_compose(name, "stop")
    return await _run_compose(name, "down")


async def compose_stop(name: str) -> Dict[str, Any]:
    """Run ``docker compose stop`` for the given stack.

    Works with ``--project-name`` for external stacks that have no
    compose file available.
    """
    return await _run_compose(name, "stop")


async def compose_restart(name: str) -> Dict[str, Any]:
    """Run ``docker compose restart`` for the given stack.

    Works with ``--project-name`` for external stacks that have no
    compose file available.
    """
    return await _run_compose(name, "restart")


async def compose_pull(name: str) -> Dict[str, Any]:
    """Pull images for a stack."""
    compose_file, _cwd = _resolve_stack_compose(name)
    if compose_file is None or not Path(compose_file).exists():
        raise FileNotFoundError(f"Stack '{name}' not found")
    return await _run_compose(name, "pull")


def get_stack_logs(stack_name: str, tail: int = 100) -> Dict[str, Any]:
    """Return the last ``tail`` log lines for a stack (``docker compose logs``).

    Works for both managed stacks (in /data/stacks/) and external stacks whose
    compose file path is derived from container labels. If ``docker compose
    logs`` fails (no compose file, command error…), falls back to aggregating
    the logs of the stack's containers.

    Returns ``{"success": bool, "output": str, "error": str}`` where
    ``output`` is the combined stdout/stderr text.
    """
    try:
        tail = max(1, int(tail))
    except (TypeError, ValueError):
        tail = 100

    args, work_dir = _resolve_compose_args(stack_name, f"logs --tail {tail} --no-color")
    proc = None
    compose_error = ""
    try:
        proc = subprocess.run(
            args,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode == 0:
            output = proc.stdout or ""
            if proc.stderr:
                output = ((output + "\n" + proc.stderr) if output else proc.stderr).strip()
            return {"success": True, "output": output, "error": ""}
        compose_error = (proc.stderr or proc.stdout or "").strip()
    except (subprocess.SubprocessError, OSError) as e:
        compose_error = str(e)

    # Fallback: aggregate the logs of the stack's containers.
    containers = get_stack_containers(stack_name)
    if not containers:
        error = compose_error or f"Stack '{stack_name}' not found or has no containers"
        return {"success": False, "output": "", "error": error}
    chunks: List[str] = []
    for c in containers:
        name = c.get("name", c.get("id", ""))
        lines = get_container_logs(c.get("id", ""), tail=tail)
        if not lines:
            continue
        chunks.append(f"── {name} ──")
        for item in lines:
            chunks.append(item.get("message", ""))
    return {"success": True, "output": "\n".join(chunks), "error": ""}


async def update_container(container_id: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    """Apply changes to a container.

    Strategy:
    - **External stack** (Compose project not managed by Docky): return error.
    - **Managed stack** (Compose project in /data/stacks/): modify the
      docker-compose.yml and redeploy the service.
    - **Standalone container** (no stack): stop, remove, recreate with new
      params, then start. Rollback on failure.
    """
    try:
        client = get_docker_client()
        c = client.containers.get(container_id)
    except Exception as e:
        return {"success": False, "error": str(e)}

    attrs = c.attrs

    # Check if external stack
    project = (attrs.get("Config", {}).get("Labels") or {}).get("com.docker.compose.project", "")
    if project:
        stacks_dir = Path(get_data_dir()) / 'stacks'
        managed = (stacks_dir / project).exists()
        if not managed:
            return {"success": False, "error": "Les stacks externes ne peuvent pas être éditées"}
        # Managed stack → modify compose file
        return await _update_compose_container(project, container_id, spec, client)

    # Standalone container → recreate
    return await _recreate_container(c, container_id, spec, client, attrs)


async def _update_compose_container(project: str, container_id: str, spec: Dict, client) -> Dict:
    """Modify docker-compose.yml and redeploy the service."""
    compose_path = Path(get_data_dir()) / 'stacks' / project / 'docker-compose.yml'
    if not compose_path.exists():
        return {"success": False, "error": "docker-compose.yml not found"}

    import yaml
    raw_compose = compose_path.read_text(encoding="utf-8")

    # Preserve the leading Docky metadata/comment block (e.g. # @name,
    # # @category). A plain ``yaml.safe_load`` + ``yaml.dump`` round-trip
    # would otherwise strip every comment from the file.
    header_lines: list[str] = []
    for line in raw_compose.splitlines():
        if line.strip().startswith("#") or line.strip() == "":
            header_lines.append(line)
        else:
            break
    header = "\n".join(header_lines)

    compose = yaml.safe_load(raw_compose)

    # Find service name from container labels
    c = client.containers.get(container_id)
    service_name = (c.attrs.get("Config", {}).get("Labels") or {}).get("com.docker.compose.service", "")
    if not service_name or service_name not in compose.get("services", {}):
        return {"success": False, "error": "Service not found in compose file"}

    service = compose["services"][service_name]

    # Apply changes
    # Image
    new_image = spec.get("image", "")
    if new_image:
        service["image"] = new_image

    # Container name
    new_name = spec.get("name", "")
    if new_name:
        service["container_name"] = new_name
    elif "container_name" in service:
        del service["container_name"]

    # Ports
    ports = []
    for p in spec.get("ports", []):
        cp = p.get("container_port", "")
        hp = p.get("host_port", "")
        if cp:
            ports.append(f"{hp}:{cp}" if hp else cp)
    if ports:
        service["ports"] = ports
    elif "ports" in service:
        del service["ports"]

    # Volumes (binds)
    volumes = []
    for v in spec.get("volumes", []):
        hp = v.get("host_path", "")
        cp = v.get("container_path", "")
        mode = v.get("mode", "rw")
        if hp and cp:
            volumes.append(f"{hp}:{cp}:{mode}" if mode != "rw" else f"{hp}:{cp}")
    if volumes:
        service["volumes"] = volumes
    elif "volumes" in service:
        del service["volumes"]

    # Env
    env = []
    for e in spec.get("env", []):
        k, v = e.get("key", ""), e.get("value", "")
        if k:
            env.append(f"{k}={v}")
    if env:
        service["environment"] = env
    elif "environment" in service:
        del service["environment"]

    # Labels
    labels = {}
    for l in spec.get("labels", []):
        k, v = l.get("key", ""), l.get("value", "")
        if k:
            labels[k] = v
    if labels:
        service["labels"] = labels
    elif "labels" in service:
        del service["labels"]

    # Restart policy
    rp = spec.get("restart_policy", "no")
    if rp and rp != "no":
        service["restart"] = rp
    elif "restart" in service:
        del service["restart"]

    # Write back, re-injecting the leading comment/metadata block so the
    # Docky metadata comments survive the round-trip.
    with open(compose_path, "w", encoding="utf-8") as f:
        if header:
            f.write(header + "\n")
        yaml.dump(compose, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    # Redeploy
    await compose_up(project)

    return {"success": True, "output": f"Stack {project} redéployée avec les modifications"}


def _ports_from_attrs(attrs: Dict) -> List[Dict[str, Any]]:
    """Rebuild the edit-spec port list from ``docker inspect`` attrs."""
    ports: List[Dict[str, Any]] = []
    seen: set = set()
    raw_ports = attrs.get("NetworkSettings", {}).get("Ports", {}) or {}
    for container_port, bindings in raw_ports.items():
        if bindings:
            for b in bindings:
                key = (container_port, b.get("HostPort", ""))
                if key not in seen:
                    seen.add(key)
                    ports.append({
                        "host_port": b.get("HostPort", ""),
                        "container_port": container_port,
                    })
        else:
            key = (container_port, "")
            if key not in seen:
                seen.add(key)
                ports.append({"host_port": "", "container_port": container_port})
    return ports


def _bind_volumes_from_attrs(attrs: Dict) -> List[Dict[str, Any]]:
    """Rebuild the edit-spec bind-mount list from ``docker inspect`` attrs."""
    volumes: List[Dict[str, Any]] = []
    for m in attrs.get("Mounts", []) or []:
        if m.get("Type") != "bind":
            continue
        volumes.append({
            "host_path": m.get("Source", ""),
            "container_path": m.get("Destination", ""),
            "mode": "ro" if "ro" in (m.get("Mode", "") or "") else "rw",
        })
    return volumes


def _env_from_config(config: Dict) -> List[Dict[str, Any]]:
    """Convert ``Config.Env`` (``KEY=value``) to the edit-spec env format."""
    env: List[Dict[str, Any]] = []
    for e in config.get("Env") or []:
        if "=" in e:
            k, v = e.split("=", 1)
            env.append({"key": k, "value": v})
        else:
            env.append({"key": e, "value": ""})
    return env


def _labels_from_config(config: Dict) -> List[Dict[str, Any]]:
    """Convert ``Config.Labels`` to the edit-spec labels format."""
    raw_labels = config.get("Labels") or {}
    return [{"key": k, "value": v} for k, v in raw_labels.items()]


def _networks_from_attrs(attrs: Dict) -> List[Dict[str, Any]]:
    """Rebuild the edit-spec network list from ``docker inspect`` attrs."""
    networks: List[Dict[str, Any]] = []
    for net_name, net_info in (attrs.get("NetworkSettings", {}).get("Networks", {}) or {}).items():
        networks.append({"name": net_name, "ip": net_info.get("IPAddress", "") or ""})
    return networks


def _devices_from_host_config(host_config: Dict) -> List[str]:
    """Convert ``HostConfig.Devices`` to the string form expected by docker-py."""
    devices: List[str] = []
    for device in host_config.get("Devices") or []:
        if isinstance(device, dict):
            path_on_host = device.get("PathOnHost", "")
            path_in_container = device.get("PathInContainer", "")
            cgroup = device.get("CgroupPermissions", "rwm")
            if path_on_host and path_in_container:
                devices.append(f"{path_on_host}:{path_in_container}:{cgroup}")
        elif isinstance(device, str) and device:
            devices.append(device)
    return devices


def _log_config_from_host_config(host_config: Dict) -> Optional[Dict[str, Any]]:
    """Convert ``HostConfig.LogConfig`` to the format expected by docker-py."""
    log_config = host_config.get("LogConfig")
    if not isinstance(log_config, dict):
        return None
    log_type = log_config.get("Type", "")
    if not log_type:
        return None
    return {
        "type": log_type,
        "config": log_config.get("Config") or {},
    }


def _ulimits_from_host_config(host_config: Dict) -> List[Dict[str, Any]]:
    """Convert ``HostConfig.Ulimits`` to the format expected by docker-py."""
    ulimits: List[Dict[str, Any]] = []
    for ulimit in host_config.get("Ulimits") or []:
        if not isinstance(ulimit, dict):
            continue
        name = ulimit.get("Name")
        if name is None:
            continue
        ulimits.append({
            "name": name,
            "soft": ulimit.get("Soft"),
            "hard": ulimit.get("Hard"),
        })
    return ulimits


async def _recreate_container(c, container_id: str, spec: Dict, client, attrs: Dict) -> Dict:
    """Stop, remove, recreate with new params. Rollback on failure."""
    new_c = None
    backup_renamed = False
    try:
        config = attrs.get("Config", {}) or {}
        host_config = attrs.get("HostConfig", {}) or {}

        old_name = c.name.lstrip("/")
        old_image = config.get("Image", "")
        new_name = spec.get("name", old_name)
        new_image = spec.get("image", old_image)

        # If only the name changed (no other settings), just rename
        name_changed = new_name != old_name
        image_changed = new_image != old_image

        old_rp = host_config.get("RestartPolicy", {}).get("Name", "no")
        new_rp = spec.get("restart_policy") or old_rp or "no"

        # Collect all spec changes to detect if anything besides name changed.
        # An explicitly provided field counts as a change even when empty
        # (clearing ports/env/... is a change).
        spec_changed = image_changed or (new_rp != old_rp)
        for key in ("ports", "volumes", "env", "labels", "networks"):
            if key in spec and spec.get(key) is not None:
                spec_changed = True
                break

        if name_changed and not spec_changed:
            # Simple rename, no recreate needed
            await asyncio.to_thread(c.rename, new_name)
            return {"success": True, "output": f"Container renommé en {new_name}"}

        # Preserve editable fields from the spec when provided; otherwise fall
        # back to the current container configuration so a partial edit does
        # not silently drop settings.
        ports_spec = spec["ports"] if "ports" in spec and spec.get("ports") is not None else _ports_from_attrs(attrs)
        volumes_spec = spec["volumes"] if "volumes" in spec and spec.get("volumes") is not None else _bind_volumes_from_attrs(attrs)
        env_spec = spec["env"] if "env" in spec and spec.get("env") is not None else _env_from_config(config)
        labels_spec = spec["labels"] if "labels" in spec and spec.get("labels") is not None else _labels_from_config(config)
        networks_spec = spec["networks"] if "networks" in spec and spec.get("networks") is not None else _networks_from_attrs(attrs)

        # Restart policy (preserve retry count when relevant)
        restart_policy = {"Name": new_rp}
        maximum_retry_count = host_config.get("RestartPolicy", {}).get("MaximumRetryCount")
        if maximum_retry_count is not None:
            restart_policy["MaximumRetryCount"] = maximum_retry_count

        # Ports
        port_bindings = {}
        for p in ports_spec:
            cp = p.get("container_port", "")
            hp = p.get("host_port", "")
            if cp and "/" in cp:
                cport, proto = cp.split("/", 1)
            elif cp:
                cport, proto = cp, "tcp"
            else:
                continue
            port_bindings[(cport, proto)] = hp or None

        # Volumes (bind mounts): docker-py maps container path -> host bind.
        volumes_dict = {}
        for v in volumes_spec:
            hp = v.get("host_path", "")
            cp = v.get("container_path", "")
            mode = v.get("mode", "rw")
            if hp and cp:
                volumes_dict[cp] = {"bind": hp, "mode": mode}

        # Env
        env_list = [f"{e['key']}={e['value']}" for e in env_spec if e.get("key")]

        # Labels
        labels_dict = {l["key"]: l["value"] for l in labels_spec if l.get("key")}

        # Networks (preserve existing attachments on recreate)
        network_kwargs = {}
        networks = [n.get("name") for n in networks_spec if n.get("name")]
        if len(networks) == 1:
            network_kwargs["network"] = networks[0]
        elif len(networks) > 1:
            network_kwargs["networks"] = networks
        else:
            network_mode = host_config.get("NetworkMode", "") or ""
            if network_mode and network_mode not in ("default", ""):
                network_kwargs["network_mode"] = network_mode

        # Preserve the remaining runtime configuration of the original
        # container that is not exposed by the edit modal.
        command = config.get("Cmd")
        entrypoint = config.get("Entrypoint")
        user = config.get("User") or ""
        working_dir = config.get("WorkingDir") or ""
        tty = bool(config.get("Tty", False))
        stdin_open = bool(config.get("OpenStdin", False))
        privileged = bool(host_config.get("Privileged", False))
        devices = _devices_from_host_config(host_config)
        dns = host_config.get("Dns") or None
        cap_add = host_config.get("CapAdd") or None
        cap_drop = host_config.get("CapDrop") or None
        ulimits = _ulimits_from_host_config(host_config)
        sysctls = host_config.get("Sysctls") or None
        extra_hosts = host_config.get("ExtraHosts") or None
        log_config = _log_config_from_host_config(host_config)
        security_opt = host_config.get("SecurityOpt") or None

        run_kwargs = {
            "image": new_image,
            "name": new_name,
            "detach": True,
            "remove": False,
            "restart_policy": restart_policy,
            "ports": port_bindings or None,
            "volumes": volumes_dict or None,
            "environment": env_list or None,
            "labels": labels_dict or None,
            "tty": tty,
            "stdin_open": stdin_open,
            "privileged": privileged,
        }
        if command is not None:
            run_kwargs["command"] = command
        if entrypoint is not None:
            run_kwargs["entrypoint"] = entrypoint
        if user:
            run_kwargs["user"] = user
        if working_dir:
            run_kwargs["working_dir"] = working_dir
        if devices:
            run_kwargs["devices"] = devices
        if dns:
            run_kwargs["dns"] = dns
        if cap_add:
            run_kwargs["cap_add"] = cap_add
        if cap_drop:
            run_kwargs["cap_drop"] = cap_drop
        if ulimits:
            run_kwargs["ulimits"] = ulimits
        if sysctls:
            run_kwargs["sysctls"] = sysctls
        if extra_hosts:
            run_kwargs["extra_hosts"] = extra_hosts
        if log_config:
            run_kwargs["log_config"] = log_config
        if security_opt:
            run_kwargs["security_opt"] = security_opt
        run_kwargs.update(network_kwargs)

        # Stop + backup name
        await asyncio.to_thread(c.stop, timeout=10)
        await asyncio.to_thread(c.rename, f"{old_name}_backup")
        backup_renamed = True
        await asyncio.to_thread(c.reload)

        # Create new container
        new_c = await asyncio.to_thread(client.containers.run, **run_kwargs)

        # Remove old container
        await asyncio.to_thread(
            lambda: client.containers.get(f"{old_name}_backup").remove(force=True)
        )

        return {"success": True, "output": f"Container {new_name} recréé avec les nouvelles configurations"}

    except Exception as e:
        # Rollback: try to restore backup container.
        #
        # First free the name that may be held by a partially-created new
        # container: docker-py may create the container and then raise (e.g.
        # during its start step) without returning it, leaving a container
        # named ``new_name`` behind. If it is not removed first, renaming the
        # backup back to ``old_name`` fails with a name conflict whenever the
        # name is unchanged (new_name == old_name).
        rollback_candidate = new_c
        if rollback_candidate is None and backup_renamed:
            # The backup was renamed away, so ``new_name`` can only be held by
            # the partially-created container (or be free). Only probe it now;
            # before the backup rename, ``get(new_name)`` could return the
            # original container itself, which must NOT be removed.
            try:
                rollback_candidate = client.containers.get(new_name)
            except Exception:
                rollback_candidate = None
        if rollback_candidate is not None:
            try:
                await asyncio.to_thread(rollback_candidate.remove, force=True)
            except Exception as rem_exc:
                logger.warning(
                    "Rollback: could not remove partially-created container: %s", rem_exc
                )
        try:
            await asyncio.to_thread(c.start)
            await asyncio.to_thread(c.rename, old_name)
        except Exception as restore_exc:
            logger.warning(
                "Rollback: could not restore container '%s': %s", old_name, restore_exc
            )
        return {"success": False, "error": str(e)}


async def _update_compose_container_image(
    container_ref: str,
    project: str,
    service: str,
    image_name: str,
    plan: Dict[str, Any],
    client,
) -> Dict[str, Any]:
    """Non-streamed, JSON variant of the single-container compose update.

    Applies the same sequence as :func:`_stream_compose_container_update`
    (pull → stop/rm → ``docker compose up -d --no-deps <service>``) and
    returns an aggregated ``{success, output, error}`` dict for JSON/LLM
    callers.  Only the targeted container/service is ever touched — no
    ``compose down``, no unscoped ``compose up -d``.
    """
    steps: List[str] = []

    # 1. Pull the service image.
    if plan.get("build_only"):
        steps.append("pull (ignoré: service construit localement)")
    else:
        pull_result = await _run_compose(project, f"pull {service}")
        if not pull_result.get("success"):
            # Fallback: direct image pull (service not resolvable).
            try:
                await asyncio.to_thread(client.images.pull, image_name)
            except Exception as e:
                return {"success": False, "error": f"Image pull failed: {e}"}
        steps.append("pull")

    # 2. Stop/rm the single container (exact ID — never a global down/up).
    try:
        c = client.containers.get(container_ref)
        try:
            c.stop(timeout=10)
        except Exception:
            pass  # already stopped → ``remove(force=True)`` still works
        c.remove(force=True)
    except Exception as e:
        return {"success": False, "error": f"Container removal failed: {e}"}
    steps.append("stop/rm")

    # 3. Recreate only the targeted service (--no-deps → no dependency churn).
    up_result = await _run_compose(project, f"up -d --no-deps {service}")
    if not up_result.get("success"):
        return {
            "success": False,
            "error": up_result.get("error", "compose up failed"),
            "command": up_result.get("command", ""),
        }
    steps.append("up -d --no-deps <service>")

    output_parts: List[str] = []
    if up_result.get("output"):
        output_parts.append(up_result["output"].strip())
    output_parts.append("Étapes exécutées: " + ", ".join(steps))
    # Le digest local vient de changer : forcer un check frais (le cache TTL
    # pourrait encore contenir les digests distants pré-pull).
    _invalidate_update_check(image_name)
    return {"success": True, "output": "\n".join(output_parts)}


async def _stream_compose_container_update(
    container_ref: str,
    project: str,
    service: str,
    image_name: str,
    idle_timeout: int,
) -> AsyncIterator[Dict[str, Any]]:
    """Stream pull → stop/rm → ``compose up -d`` for ONE stack container.

    Every step is labelled so the user sees exactly what runs:

    1. ``docker compose pull <service>`` — fallback ``docker pull <image>``
       when the service cannot be targeted.  Build-only services (``build:``
       with no ``image:``) skip the pull with an explicit message.
    2. ``docker stop <container_ref>`` then ``docker rm -f <container_ref>`` —
       always addressed by the exact container ID, never a global down/up.
    3. ``docker compose up -d --no-deps <service>`` — scoped to the single
       service so the project's other containers are not recreated.

    Isolation contract: this generator never runs ``compose down`` and never
    runs an unscoped ``compose up -d``.  On a pull failure it stops before
    removing/recreating anything; on a removal failure it reports a clear
    error.  The final event is a ``result`` dict.
    """
    collect: List[str] = []
    plan = _compose_service_update_plan(project, service)
    compose_available = bool(plan["compose_available"])
    short_ref = container_ref[:12] if len(container_ref) > 12 else container_ref

    # ---------- Step 1: pull ----------
    if compose_available and plan["build_only"]:
        # Locally-built service (build:) with no registry image to pull.
        msg = (
            f"── Service '{service}' est construit localement (build:) : "
            f"pas d'image de registre, étape pull ignorée ──"
        )
        collect.append(msg)
        yield {"type": STREAM_EVENT_OUTPUT, "line": msg}
    else:
        result = None
        if compose_available:
            args, work_dir = _resolve_compose_args(project, f"pull {service}")
            async for evt in _stream_command_step(
                args, work_dir, f"── docker compose pull {service} ──", idle_timeout, collect
            ):
                if evt["type"] == STREAM_EVENT_RESULT:
                    result = evt
                else:
                    yield evt
            if not result or not result["success"]:
                # Service not resolvable / compose pull unsupported → direct pull.
                args = ["docker", "pull", image_name]
                result = None
                async for evt in _stream_command_step(
                    args, None, f"── Fallback: docker pull {image_name} ──", idle_timeout, collect
                ):
                    if evt["type"] == STREAM_EVENT_RESULT:
                        result = evt
                    else:
                        yield evt
        else:
            # No compose file available: pull the image directly.
            args = ["docker", "pull", image_name]
            async for evt in _stream_command_step(
                args, None, f"── docker pull {image_name} ──", idle_timeout, collect
            ):
                if evt["type"] == STREAM_EVENT_RESULT:
                    result = evt
                else:
                    yield evt

        if not result or not result["success"]:
            # Pull failed → stop here: do NOT remove or recreate the container.
            error = (result or {}).get("error", "Image pull failed")
            yield {
                "type": STREAM_EVENT_RESULT,
                "success": False,
                "output": "\n".join(collect),
                "error": error,
                "command": (result or {}).get("command", ""),
            }
            return

    # ---------- Step 2: stop/rm the single container ----------
    async for evt in _stream_command_step(
        ["docker", "stop", container_ref], None, f"── docker stop {short_ref} ──", idle_timeout, collect
    ):
        if evt["type"] == STREAM_EVENT_RESULT:
            pass  # graceful stop; ``rm -f`` below handles a still-running one
        else:
            yield evt

    rm_result = None
    async for evt in _stream_command_step(
        ["docker", "rm", "-f", container_ref], None, f"── docker rm -f {short_ref} ──", idle_timeout, collect
    ):
        if evt["type"] == STREAM_EVENT_RESULT:
            rm_result = evt
        else:
            yield evt
    if rm_result is None or not rm_result["success"]:
        error = (rm_result or {}).get("error", "Container removal failed")
        yield {
            "type": STREAM_EVENT_RESULT,
            "success": False,
            "output": "\n".join(collect),
            "error": error,
            "command": (rm_result or {}).get("command", ""),
        }
        return

    # ---------- Step 3: recreate the single service ----------
    # ``docker compose up -d --no-deps <service>`` recreates only the targeted
    # service container; ``--no-deps`` prevents compose from starting or
    # recreating the project's other services.  Modern Docker Compose (v2, the
    # ``docker compose`` plugin used here) supports service targeting, so no
    # unscoped ``up -d`` fallback is performed — that would recreate the whole
    # project and break the isolation guarantee.
    args, work_dir = _resolve_compose_args(project, f"up -d --no-deps {service}")
    result = None
    async for evt in _stream_command_step(
        args, work_dir, f"── docker compose up -d --no-deps {service} ──", idle_timeout, collect
    ):
        if evt["type"] == STREAM_EVENT_RESULT:
            result = evt
        else:
            yield evt
    if result is None or not result["success"]:
        error = (result or {}).get("error", "compose up failed")
        yield {
            "type": STREAM_EVENT_RESULT,
            "success": False,
            "output": "\n".join(collect),
            "error": error,
            "command": (result or {}).get("command", ""),
        }
        return

    # Le digest local vient de changer : forcer un check frais (le cache TTL
    # pourrait encore contenir les digests distants pré-pull).
    _invalidate_update_check(image_name)
    yield {
        "type": STREAM_EVENT_RESULT,
        "success": True,
        "output": "\n".join(collect),
        "error": "",
        "command": "",
    }


async def update_container_image(container_id: str) -> Dict[str, Any]:
    """Pull the latest image for a single container and recreate it.

    - **Stack container** (Compose project with a resolvable compose file):
      pulls the service image, stops/removes the single container, then runs
      ``docker compose up -d --no-deps <service>`` so only that service is
      recreated.  Never runs ``compose down`` nor an unscoped ``compose up -d``.
    - **Standalone container** (or a stack container whose compose file cannot
      be resolved): pulls the image, then recreates the container with its
      current configuration (ports, volumes, env, networks, labels).
    """
    try:
        client = get_docker_client()
        c = client.containers.get(container_id)
    except Exception as e:
        return {"success": False, "error": str(e)}

    attrs = c.attrs
    labels = attrs.get("Config", {}).get("Labels") or {}
    project = labels.get("com.docker.compose.project", "")
    service = labels.get("com.docker.compose.service", "")
    image_name = (attrs.get("Config", {}).get("Image", "") or "").strip()
    if not image_name:
        return {"success": False, "error": "No image configured for this container"}

    if project:
        plan = _compose_service_update_plan(project, service)
        if plan["compose_available"] and plan["service_declared"]:
            return await _update_compose_container_image(
                c.id, project, service, image_name, plan, client
            )
        # Compose file unresolvable or service no longer declared → fall
        # through to the per-container recreate below (only touches this
        # container, never a global down/up).

    # 1. Pull the new image
    try:
        await asyncio.to_thread(client.images.pull, image_name)
    except Exception as e:
        return {"success": False, "error": f"Image pull failed: {e}"}

    # 2. Recreate the container with its current configuration
    spec = _get_container_full_spec(container_id)
    if spec is None:
        return {"success": False, "error": "Container not found after pull"}
    result = await _recreate_container(c, container_id, spec, client, attrs)
    if result.get("success"):
        # Le digest local vient de changer : forcer un check frais (le cache
        # TTL pourrait encore contenir les digests distants pré-pull).
        _invalidate_update_check(image_name)
    return result


async def stream_update_container_image(
    container_id: str, idle_timeout: int = STREAM_IDLE_TIMEOUT
) -> AsyncIterator[Dict[str, Any]]:
    """Stream an image update for a single container in real time.

    - **Stack container** (Compose project): streams the labelled sequence
      ``docker compose pull <service>`` (fallback ``docker pull <image>``) →
      ``docker stop/rm <container_id>`` → ``docker compose up -d --no-deps
      <service>``.  Only the targeted container/service is touched.
    - **Standalone container** (or a stack container whose compose file cannot
      be resolved): streams the ``docker pull`` progress, then recreates the
      container with its current configuration.

    Yields ``output`` events for each line and a final ``result`` event.
    """
    try:
        client = get_docker_client()
        c = client.containers.get(container_id)
    except Exception as e:
        yield {"type": STREAM_EVENT_RESULT, "success": False, "output": "", "error": str(e), "command": ""}
        return

    attrs = c.attrs
    labels = attrs.get("Config", {}).get("Labels") or {}
    project = labels.get("com.docker.compose.project", "")
    service = labels.get("com.docker.compose.service", "")
    image_name = (attrs.get("Config", {}).get("Image", "") or "").strip()
    if not image_name:
        yield {
            "type": STREAM_EVENT_RESULT,
            "success": False,
            "output": "",
            "error": "No image configured for this container",
            "command": "",
        }
        return

    if project:
        plan = _compose_service_update_plan(project, service)
        if plan["compose_available"] and plan["service_declared"]:
            async for evt in _stream_compose_container_update(
                c.id, project, service, image_name, idle_timeout
            ):
                yield evt
            return
        # Compose file unresolvable or service no longer declared → fall
        # through to the per-container pull + recreate below (only touches
        # this container, never a global down/up).

    output_lines: list = []
    pull_label = f"── docker pull {image_name} ──"
    output_lines.append(pull_label)
    yield {"type": STREAM_EVENT_OUTPUT, "line": pull_label}
    try:
        async for line in _run_command_stream(["docker", "pull", image_name], idle_timeout=idle_timeout):
            output_lines.append(line)
            yield {"type": STREAM_EVENT_OUTPUT, "line": line}
    except Exception as e:
        yield {
            "type": STREAM_EVENT_RESULT,
            "success": False,
            "output": "\n".join(output_lines),
            "error": f"Image pull failed: {e}",
            "command": "docker pull " + image_name,
        }
        return

    # 2. Recreate the container with its current configuration
    try:
        spec = _get_container_full_spec(container_id)
        if spec is None:
            yield {
                "type": STREAM_EVENT_RESULT,
                "success": False,
                "output": "\n".join(output_lines),
                "error": "Container not found after pull",
                "command": "",
            }
            return
        result = await _recreate_container(c, container_id, spec, client, attrs)
    except Exception as e:
        yield {
            "type": STREAM_EVENT_RESULT,
            "success": False,
            "output": "\n".join(output_lines),
            "error": str(e),
            "command": "",
        }
        return

    if not result.get("success"):
        yield {
            "type": STREAM_EVENT_RESULT,
            "success": False,
            "output": "\n".join(output_lines),
            "error": result.get("error", "Recreate failed"),
            "command": "",
        }
        return

    recreate_output = result.get("output", "") or ""
    if recreate_output:
        output_lines.append(recreate_output)
        yield {"type": STREAM_EVENT_OUTPUT, "line": recreate_output}
    # Le digest local vient de changer : forcer un check frais (le cache TTL
    # pourrait encore contenir les digests distants pré-pull).
    _invalidate_update_check(image_name)
    yield {
        "type": STREAM_EVENT_RESULT,
        "success": True,
        "output": "\n".join(output_lines),
        "error": "",
        "command": "",
    }


async def update_stack(name: str) -> Dict[str, Any]:
    """Update a stack: ``docker compose pull`` then ``docker compose up -d``.

    ``up -d --remove-orphans`` supprime les containers devenus orphelins
    (un service retiré du compose est donc supprimé au prochain update).

    Returns a dict with ``success`` and ``output``.
    Raises ``FileNotFoundError`` if the stack directory does not exist.
    """
    compose_file, _cwd = _resolve_stack_compose(name)
    if compose_file is None or not Path(compose_file).exists():
        raise FileNotFoundError(f"Stack '{name}' not found")
    pull_result = await _run_compose(name, "pull")
    up_result = await _run_compose(name, "up -d --remove-orphans")
    success = pull_result.get("success", False) and up_result.get("success", False)
    output_parts: list[str] = []
    if pull_result.get("output"):
        output_parts.append("--- docker compose pull ---\n" + pull_result["output"])
    if pull_result.get("error"):
        output_parts.append("--- docker compose pull (stderr) ---\n" + pull_result["error"])
    if up_result.get("output"):
        output_parts.append("--- docker compose up -d --remove-orphans ---\n" + up_result["output"])
    if up_result.get("error"):
        output_parts.append("--- docker compose up -d --remove-orphans (stderr) ---\n" + up_result["error"])
    if success:
        # Les digests locaux viennent de changer : forcer un check frais (le
        # cache TTL 300 s pourrait encore contenir les digests distants
        # pré-pull et laisser le badge "update dispo" visible).
        _invalidate_stack_update_cache(name)
    return {
        "success": success,
        "output": "\n".join(output_parts),
    }


def check_stack_update(stack_name: str) -> Dict[str, Any]:
    """Check whether any service image of a stack has an update available.

    Reads the stack compose file, inspects each service image via
    ``docker manifest inspect --verbose`` plus the daemon's distribution
    endpoint (no pull) and compares it with the local image digest.  Returns
    ``update_available`` plus per-service details.
    """
    compose_file, _cwd = _resolve_stack_compose(stack_name)
    if compose_file is None or not Path(compose_file).exists():
        raise FileNotFoundError(f"Stack '{stack_name}' not found")

    import yaml
    try:
        raw_compose = compose_file.read_text(encoding="utf-8")
        compose = yaml.safe_load(raw_compose) or {}
    except Exception as e:
        return {
            "update_available": False,
            "services": {},
            "error": f"Failed to read compose file: {e}",
        }

    if not isinstance(compose, dict):
        return {
            "update_available": False,
            "services": {},
            "error": "Invalid compose file",
        }

    services = compose.get("services") or {}
    if not isinstance(services, dict):
        return {
            "update_available": False,
            "services": {},
            "error": "Invalid compose services section",
        }
    result_services: Dict[str, Any] = {}
    any_update = False

    for service_name, service in services.items():
        if not isinstance(service, dict):
            continue
        image = service.get("image")
        if not isinstance(image, str) or not image.strip():
            continue
        image_name = image.strip()
        # Compose variable interpolation cannot be resolved without the
        # stack environment; skip it silently rather than failing the check.
        if "${" in image_name:
            continue

        repository, tag = _split_image_reference(image_name)
        local_digests = _local_repo_digests_for_image(image_name)
        remote_info = _remote_manifest_check(repository, tag)
        remote_digests = remote_info.get("digests", [])
        remote_digest = remote_digests[0] if remote_digests else None

        # Same logic as :func:`check_image_update`: false as soon as ANY local
        # digest matches; the remote list merges index + child digests.  If the
        # index digest is unavailable and nothing matches, prefer a false
        # negative (classic store local digest may be the manifest-list index).
        if not local_digests:
            service_update = False
            reason = "no_local_repo_digest"
        elif any(d in remote_digests for d in local_digests):
            service_update = False
            reason = "digests_match"
        elif not remote_digests:
            service_update = False
            reason = "no_remote_digests"
        elif not remote_info.get("index_digest"):
            service_update = False
            reason = "remote_index_digest_unavailable"
        elif not remote_info.get("child_digests"):
            service_update = False
            reason = "remote_child_digests_unavailable"
        else:
            service_update = True
            reason = "digest_mismatch"
        any_update = any_update or service_update

        logger.info(
            "update-check stack=%s service=%s image=%s tag=%s local_digests=%s "
            "remote_digests=%s update_available=%s reason=%s",
            stack_name, service_name, image_name, tag or "", local_digests,
            remote_digests, service_update, reason,
        )

        if service_update:
            logger.warning(
                "UPDATE_CHECK_STACK_RESULT stack=%s service=%s image=%s tag=%s "
                "local_digests=%s remote_digests=%s remote_index_digest=%s "
                "manifest_type=%s platforms=%s manifest_count=%s "
                "update_available=true reason=%s",
                stack_name, service_name, image_name, tag or "",
                _short_digests(local_digests), _short_digests(remote_digests),
                _short_digest(remote_info.get("index_digest")),
                remote_info.get("media_type") or "unknown",
                ",".join(remote_info.get("platforms") or []) or "unknown",
                len(remote_info.get("child_digests") or []),
                reason,
            )

        result_services[service_name] = {
            "update_available": service_update,
            "local_digest": local_digests[0] if local_digests else None,
            "remote_digest": remote_digest,
            "image": image_name,
        }

    return {
        "update_available": any_update,
        "services": result_services,
    }


async def system_prune() -> Dict[str, Any]:
    """Docker system prune - remove unused containers, images, volumes, networks."""
    proc = await asyncio.create_subprocess_exec(
        "docker", "system", "prune", "-f",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    success = proc.returncode == 0
    output = ""
    if stdout_bytes:
        output += stdout_bytes.decode("utf-8", errors="replace")
    if stderr_bytes:
        output += stderr_bytes.decode("utf-8", errors="replace")
    return {"success": success, "output": output}


# ---------------------------------------------------------------------------
# Stack file management
# ---------------------------------------------------------------------------


# Liste blanche des fichiers éditables dans le panneau d'édition compose :
# fichiers Docker Compose (et variantes de nommage) + le fichier .env.
# Tout autre fichier présent dans le dossier de la stack (last_results.json,
# *.md, *.log, …) est masqué de la liste par défaut. Le paramètre
# ``include_hidden`` de :func:`get_stack_files` permet de récupérer la liste
# complète (ex. pour le toggle « afficher tous les fichiers » ou pour l'outil
# LLM ``get_stack_files`` qui veut conserver la vision complète du dossier).
_EDITABLE_STACK_FILES = frozenset({
    "docker-compose.yml",
    "docker-compose.yaml",
    "docker-compose.override.yml",
    "docker-compose.override.yaml",
    "compose.yml",
    "compose.yaml",
    "compose.override.yml",
    "compose.override.yaml",
    ".env",
})


def is_editable_stack_file(filename: str) -> bool:
    """Return True if *filename* should be shown in the compose editor.

    Only Docker Compose files (and common naming variants) plus ``.env`` are
    considered editable from the UI. Generated/auxiliary files (last_results.json,
    README.md, *.log, …) are hidden by default.
    """
    return filename in _EDITABLE_STACK_FILES


def get_stack_files(stack_name: str, include_hidden: bool = False) -> List[Dict[str, Any]]:
    """List files in a stack directory (non-recursive, one level).

    By default only the *editable* files (Docker Compose files + ``.env``) are
    returned — the UI must not expose generated/auxiliary files such as
    ``last_results.json``. Pass ``include_hidden=True`` to get the complete
    list (used by the frontend toggle and the LLM ``get_stack_files`` tool).

    Returns a list of dicts with ``name``, ``size`` and ``is_dir``.
    """
    base = _stack_dir(stack_name)
    if not base.exists():
        raise FileNotFoundError(f"Stack '{stack_name}' not found")

    result: List[Dict[str, Any]] = []
    for entry in sorted(base.iterdir(), key=lambda e: e.name):
        if entry.is_dir():
            continue
        if not include_hidden and not is_editable_stack_file(entry.name):
            continue
        result.append({
            "name": entry.name,
            "size": entry.stat().st_size,
            "is_dir": False,
        })
    return result


def get_stack_file(stack_name: str, filename: str) -> str:
    """Read and return the content of a file in a stack directory."""
    target = safe_join(stack_name, filename)
    if not target.exists():
        raise FileNotFoundError(f"File '{filename}' not found in stack '{stack_name}'")
    return target.read_text(encoding="utf-8")


def save_stack_file(stack_name: str, filename: str, content: str) -> Path:
    """Write *content* to a file in a stack directory. Creates the file if it
    does not exist. Returns the path written."""
    base = _stack_dir(stack_name)
    if not base.exists():
        raise FileNotFoundError(f"Stack '{stack_name}' not found")
    target = safe_join(stack_name, filename)
    target.write_text(content, encoding="utf-8")
    from datetime import datetime
    _git_save(stack_name, f"Save {filename} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return target


def create_stack(name: str, compose_content: str, env_content: str = "") -> Dict[str, Any]:
    """Create a new stack directory with docker-compose.yml and optionally .env.

    Returns a dict with ``name`` and ``path``.
    """
    validate_stack_name(name)
    base = (get_stacks_dir() / name).resolve()
    if base.exists():
        raise FileExistsError(f"Stack '{name}' already exists")
    base.mkdir(parents=True, exist_ok=False)
    compose_path = base / "docker-compose.yml"
    compose_path.write_text(compose_content, encoding="utf-8")
    if env_content:
        env_path = base / ".env"
        env_path.write_text(env_content, encoding="utf-8")
    _git_init()
    _git_save(name, f"Création de {name}")
    return {"name": name, "path": str(base)}


async def delete_stack(name: str) -> Dict[str, Any]:
    """Delete a stack: stop/remove containers, then delete the stack directory."""
    base = _stack_dir(name)
    if not base.exists():
        raise FileNotFoundError(f"Stack '{name}' not found")
    stacks_dir = get_stacks_dir().resolve()
    if base != stacks_dir and stacks_dir not in base.parents:
        raise ValueError("Refusing to delete: path outside stacks directory")

    # 1. Stop and remove containers before deleting files
    try:
        await compose_down(name)
    except Exception as e:
        logger.warning("compose_down failed during stack deletion of '%s': %s", name, e)

    # 2. Remove the stack directory (offload blocking I/O to a thread)
    await asyncio.to_thread(shutil.rmtree, base)

    # 3. Commit the deletion in git history (offload subprocess calls too)
    try:
        stacks_dir = Path(get_data_dir()) / 'stacks'
        # Make sure the git repo exists before writing a commit: a fresh
        # deployment only creates ``data/stacks/.git`` lazily on the first
        # stack creation/save, so without this a deletion is never tracked.
        if not (stacks_dir / '.git').exists():
            subprocess.run(["git", "init"], cwd=str(stacks_dir), capture_output=True)
            subprocess.run(["git", "config", "user.name", "Docky"], cwd=str(stacks_dir), capture_output=True)
            subprocess.run(["git", "config", "user.email", "docky@local"], cwd=str(stacks_dir), capture_output=True)
        await asyncio.to_thread(
            subprocess.run, ["git", "add", "-A", str(stacks_dir)], cwd=str(stacks_dir), capture_output=True
        )
        await asyncio.to_thread(
            subprocess.run, ["git", "commit", "-m", f"Suppression de {name}", "--allow-empty"], cwd=str(stacks_dir), capture_output=True
        )
    except Exception as exc:
        logger.warning("git commit of stack deletion '%s' failed: %s", name, exc)

    return {"name": name, "deleted": True}


async def deploy_stack(name: str) -> Dict[str, Any]:
    """Deploy a stack: ``docker compose down`` then ``docker compose up -d``.

    ``down`` (sans ``-v`` : les volumes nommés sont conservés) puis
    ``up -d --remove-orphans`` : un service retiré du compose est donc
    supprimé au prochain déploiement.

    Returns a dict with ``success``, ``output`` and ``error``.
    Raises ``FileNotFoundError`` if the stack directory does not exist.
    """
    compose_file, _cwd = _resolve_stack_compose(name)
    if compose_file is None or not Path(compose_file).exists():
        raise FileNotFoundError(f"Stack '{name}' not found")
    down_result = await compose_down(name)
    up_result = await compose_up(name)
    success = up_result.get("success", False)
    output_parts = []
    if down_result.get("output"):
        output_parts.append("--- docker compose down ---\n" + down_result["output"])
    if down_result.get("error"):
        output_parts.append("--- docker compose down (stderr) ---\n" + down_result["error"])
    if up_result.get("output"):
        output_parts.append("--- docker compose up -d --remove-orphans ---\n" + up_result["output"])
    if up_result.get("error"):
        output_parts.append("--- docker compose up -d --remove-orphans (stderr) ---\n" + up_result["error"])
    if success:
        # Le déploiement recrée les containers à partir des images locales. Si
        # une de ces images a changé entre-temps (pull manuel, update d'un
        # container), le badge peut être faux ; invalider est trivial et sans
        # risque (le prochain check relit simplement le registre).
        _invalidate_stack_update_cache(name)
    return {
        "success": success,
        "output": "\n".join(output_parts),
        "error": up_result.get("error", "") if not success else "",
        "command": up_result.get("command", ""),
    }


def set_file_permissions(stack_name: str, filename: str, mode: str) -> Dict[str, Any]:
    """Change the permissions (chmod) of a file in a stack directory.

    *mode* can be a string like ``"644"`` or an integer like ``0o644``.
    """
    target = safe_join(stack_name, filename)
    if not target.exists():
        raise FileNotFoundError(f"File '{filename}' not found in stack '{stack_name}'")
    if isinstance(mode, str):
        mode_str = mode.strip()
        if mode_str.startswith("0o") or mode_str.startswith("0O"):
            mode_int = int(mode_str, 8)
        else:
            mode_int = int(mode_str, 8)
    else:
        mode_int = int(mode)
    os.chmod(target, mode_int)
    new_mode = oct(target.stat().st_mode & 0o777)
    return {"name": filename, "mode": new_mode}


# ---------------------------------------------------------------------------
# Git history for stacks
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Update check
# ---------------------------------------------------------------------------
# Fonctions déplacées vers agent/docker/update_check.py et ré-exportées ici
# (façade) pour préserver routes.py/main.py et les monkeypatchs des tests
# ciblant agent.docker_manager.*.
from agent.docker.update_check import (
    _UPDATE_CHECK_TTL,
    _update_check_cache,
    _canonical_repository,
    _clean_update_check_cache,
    _dedupe_preserve_order,
    _extract_remote_digests,
    _invalidate_stack_update_cache,
    _invalidate_update_check,
    _local_repo_digests,
    _local_repo_digests_for_image,
    _remote_distribution_info,
    _remote_manifest_check,
    _remote_manifest_digests,
    _short_digest,
    _short_digests,
    _split_image_reference,
    _update_cache_key,
    _update_check_cache_info,
    check_image_update,
)

