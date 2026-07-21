"""Docker SDK client utilities for the Docky Agent service.

Adapted from ``app/docker_manager/client.py`` — all Docker SDK functions
needed by the agent: container management, stack management, file editing,
ports scanning and update checks.
"""

import asyncio
import logging
import os
import re
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

import docker
from docker.errors import DockerException, NotFound, APIError

from agent.config import get_data_dir

logger = logging.getLogger(__name__)

# Pseudo-stack name used to group containers that are not part of any
# Docker Compose project (i.e. standalone containers).
STANDALONE_STACK_NAME = "Standalone"


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


def watch_docker_events() -> Generator[Dict[str, Any], None, None]:
    """Generate Docker events as they happen (blocking generator)."""
    client = get_docker_client()
    for event in client.events(decode=True):
        if isinstance(event, dict):
            yield event


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
    except DockerException:
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
    except Exception:
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
        # Use low-level API to get raw multiplexed bytes (8-byte headers)
        raw = client.api.logs(
            container_id,
            stdout=True,
            stderr=True,
            tail=tail,
            timestamps=True,
        )
    except (NotFound, DockerException, APIError):
        return []

    if not raw or not isinstance(raw, bytes):
        return []

    result: List[Dict] = []
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
                yield chunk.decode("utf-8", errors="replace").rstrip("\n")
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


def exec_in_container(container_id: str, command: str, tty: bool = False) -> str:
    """Execute a command in a container and return the output (one-shot)."""
    try:
        client = get_docker_client()
        c = client.containers.get(container_id)
        result = c.exec_run(command, tty=tty)
        output = result.output
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return output
    except (NotFound, DockerException, APIError) as e:
        return f"[error] {e}"


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

def get_stacks_dir() -> Path:
    """Return the path to the stacks directory inside the data dir."""
    return get_data_dir() / "stacks"


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
    except DockerException:
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


async def _run_compose(stack_name: str, command: str, timeout: int = 300) -> Dict[str, Any]:
    """Run a docker compose subcommand for a stack (non-blocking).

    Works for both managed stacks (in /data/stacks/) and external stacks
    whose compose file path is derived from container labels.

    For managed stacks (or external stacks whose compose file was found via
    container labels), the ``-f`` flag is used.

    For external stacks whose compose file could not be located, the
    ``--project-name`` flag is used instead, which allows commands such as
    ``stop``, ``restart`` and ``start`` to operate on the existing containers
    without needing the compose file.

    Uses :func:`asyncio.create_subprocess_exec` so the FastAPI event loop
    is not blocked while Docker pulls images or starts containers.
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
            return {"success": True, "output": stdout, "command": full_cmd}
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


async def compose_start(name: str) -> Dict[str, Any]:
    """Start existing containers for the given stack.

    Works with ``--project-name`` for external stacks that have no
    compose file available.
    """
    return await _run_compose(name, "start")


async def compose_up(name: str) -> Dict[str, Any]:
    """Run ``docker compose up -d`` for the given stack.

    For managed stacks (or external stacks with a known compose file),
    this runs ``docker compose up -d``.  For external stacks whose
    compose file cannot be located, it falls back to ``docker compose
    --project-name {name} start`` which starts existing containers without
    needing the compose file.
    """
    compose_file, _cwd = _resolve_stack_compose(name)
    if compose_file is None or not Path(compose_file).exists():
        # External stack: use 'start' instead of 'up -d'
        return await _run_compose(name, "start")
    return await _run_compose(name, "up -d")


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
    with open(compose_path) as f:
        compose = yaml.safe_load(f)

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

    # Write back
    with open(compose_path, "w") as f:
        yaml.dump(compose, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    # Redeploy
    await compose_up(project)

    return {"success": True, "output": f"Stack {project} redéployée avec les modifications"}


async def _recreate_container(c, container_id: str, spec: Dict, client, attrs: Dict) -> Dict:
    """Stop, remove, recreate with new params. Rollback on failure."""
    try:
        old_name = c.name.lstrip("/")
        old_image = attrs.get("Config", {}).get("Image", "")
        new_name = spec.get("name", old_name)
        new_image = spec.get("image", old_image)

        # If only the name changed (no other settings), just rename
        name_changed = new_name != old_name
        image_changed = new_image != old_image

        # Collect all spec changes to detect if anything besides name changed
        spec_changed = image_changed
        old_rp = attrs.get("HostConfig", {}).get("RestartPolicy", {}).get("Name", "no")
        new_rp = spec.get("restart_policy", old_rp)
        if new_rp != old_rp:
            spec_changed = True
        # Check ports, volumes, env, labels for changes by comparing to current spec
        # (Fetching old spec fully would be heavy, so we check if any of these are provided)
        if spec.get("ports") or spec.get("volumes") or spec.get("env") or spec.get("labels"):
            spec_changed = True

        if name_changed and not spec_changed:
            # Simple rename, no recreate needed
            await asyncio.to_thread(c.rename, new_name)
            return {"success": True, "output": f"Container renommé en {new_name}"}

        # Build docker run params
        run_kwargs = {
            "image": new_image,
            "name": new_name,
            "detach": True,
        }

        # Restart policy
        run_kwargs["restart_policy"] = {"Name": spec.get("restart_policy", "no")}

        # Ports
        port_bindings = {}
        for p in spec.get("ports", []):
            cp = p.get("container_port", "")
            hp = p.get("host_port", "")
            if cp and "/" in cp:
                cport, proto = cp.split("/", 1)
            elif cp:
                cport, proto = cp, "tcp"
            else:
                continue
            if hp:
                port_bindings[(cport, proto)] = hp
            else:
                port_bindings[(cport, proto)] = None

        # Volumes
        volumes_dict = {}
        binds = []
        for v in spec.get("volumes", []):
            hp = v.get("host_path", "")
            cp = v.get("container_path", "")
            mode = v.get("mode", "rw")
            if hp and cp:
                binds.append(f"{hp}:{cp}:{mode}" if mode != "rw" else f"{hp}:{cp}")
                volumes_dict[cp] = {"bind": cp, "mode": mode}

        # Env
        env_list = [f"{e['key']}={e['value']}" for e in spec.get("env", []) if e.get("key")]

        # Labels
        labels_dict = {l["key"]: l["value"] for l in spec.get("labels", []) if l.get("key")}

        # Stop + backup name
        await asyncio.to_thread(c.stop, timeout=10)
        await asyncio.to_thread(c.rename, f"{old_name}_backup")
        await asyncio.to_thread(c.reload)

        # Create new container
        new_c = await asyncio.to_thread(
            client.containers.run,
            new_image,
            detach=True,
            name=new_name,
            restart_policy=run_kwargs["restart_policy"],
            ports=port_bindings or None,
            volumes=volumes_dict or None,
            environment=env_list or None,
            labels=labels_dict or None,
            remove=False,
        )

        # Remove old container
        await asyncio.to_thread(
            lambda: client.containers.get(f"{old_name}_backup").remove(force=True)
        )

        return {"success": True, "output": f"Container {new_name} recréé avec les nouvelles configurations"}

    except Exception as e:
        # Rollback: try to restore backup container
        try:
            await asyncio.to_thread(c.start)
            await asyncio.to_thread(c.rename, old_name)
        except Exception:
            pass
        return {"success": False, "error": str(e)}


async def update_stack(name: str) -> Dict[str, Any]:
    """Update a stack: ``docker compose pull`` then ``docker compose up -d``.

    Returns a dict with ``success`` and ``output``.
    Raises ``FileNotFoundError`` if the stack directory does not exist.
    """
    compose_file, _cwd = _resolve_stack_compose(name)
    if compose_file is None or not Path(compose_file).exists():
        raise FileNotFoundError(f"Stack '{name}' not found")
    pull_result = await _run_compose(name, "pull")
    up_result = await _run_compose(name, "up -d")
    success = pull_result.get("success", False) and up_result.get("success", False)
    output_parts: list[str] = []
    if pull_result.get("output"):
        output_parts.append("--- docker compose pull ---\n" + pull_result["output"])
    if pull_result.get("error"):
        output_parts.append("--- docker compose pull (stderr) ---\n" + pull_result["error"])
    if up_result.get("output"):
        output_parts.append("--- docker compose up -d ---\n" + up_result["output"])
    if up_result.get("error"):
        output_parts.append("--- docker compose up -d (stderr) ---\n" + up_result["error"])
    return {
        "success": success,
        "output": "\n".join(output_parts),
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

_STACK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9.][A-Za-z0-9_.-]*$")


def validate_stack_name(name: str) -> str:
    """Return the stack name if valid, raise ValueError otherwise."""
    if not name or not _STACK_NAME_RE.match(name):
        raise ValueError(f"Invalid stack name: {name!r}")
    if ".." in name or "/" in name or "\\" in name:
        raise ValueError(f"Invalid stack name: {name!r}")
    return name


_validate_stack_name = validate_stack_name


def validate_filename(filename: str) -> str:
    """Validate a filename within a stack directory."""
    if not filename:
        raise ValueError("Empty filename")
    if filename == "." or filename == "..":
        raise ValueError(f"Invalid filename: {filename!r}")
    if "/" in filename or "\\" in filename:
        raise ValueError(f"Filename must not contain path separators: {filename!r}")
    if ".." in filename:
        raise ValueError(f"Filename must not contain '..': {filename!r}")
    if not _SAFE_FILENAME_RE.match(filename):
        raise ValueError(f"Invalid filename: {filename!r}")
    return filename


_validate_filename = validate_filename


def _stack_dir(name: str) -> Path:
    """Return the resolved path to a stack directory."""
    validate_stack_name(name)
    return (get_stacks_dir() / name).resolve()


def safe_join(stack_name: str, filename: str) -> Path:
    """Join *filename* to the stack directory and verify the resolved path
    stays inside the stack directory.

    Raises ``ValueError`` if the stack name or filename is invalid, or if a
    path traversal attempt is detected. Returns the resolved ``Path``.
    """
    validate_filename(filename)
    base = _stack_dir(stack_name)
    target = (base / filename).resolve()
    if base != target and base not in target.parents:
        raise ValueError("Path traversal detected")
    return target


_stack_file_path = safe_join


def get_stack_files(stack_name: str) -> List[Dict[str, Any]]:
    """List files in a stack directory (non-recursive, one level).

    Returns a list of dicts with ``name``, ``size`` and ``is_dir``.
    """
    base = _stack_dir(stack_name)
    if not base.exists():
        raise FileNotFoundError(f"Stack '{stack_name}' not found")

    result: List[Dict[str, Any]] = []
    for entry in sorted(base.iterdir(), key=lambda e: e.name):
        if entry.is_dir():
            continue
        result.append({
            "name": entry.name,
            "size": entry.stat().st_size,
            "is_dir": False,
        })
    return result


def get_stack_file(stack_name: str, filename: str) -> str:
    """Read and return the content of a file in a stack directory."""
    target = _stack_file_path(stack_name, filename)
    if not target.exists():
        raise FileNotFoundError(f"File '{filename}' not found in stack '{stack_name}'")
    return target.read_text(encoding="utf-8")


def save_stack_file(stack_name: str, filename: str, content: str) -> Path:
    """Write *content* to a file in a stack directory. Creates the file if it
    does not exist. Returns the path written."""
    base = _stack_dir(stack_name)
    if not base.exists():
        raise FileNotFoundError(f"Stack '{stack_name}' not found")
    target = _stack_file_path(stack_name, filename)
    target.write_text(content, encoding="utf-8")
    from datetime import datetime
    _git_save(stack_name, f"Save {filename} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return target


def create_stack(name: str, compose_content: str, env_content: str = "") -> Dict[str, Any]:
    """Create a new stack directory with docker-compose.yml and optionally .env.

    Returns a dict with ``name`` and ``path``.
    """
    _validate_stack_name(name)
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

    # 2. Remove the stack directory
    shutil.rmtree(base)
    return {"name": name, "deleted": True}


def import_stack(source_path: str, stack_name: str = None, dry_run: bool = False) -> dict:
    """Import a stack from an external directory (e.g. Dockge).
    Copies docker-compose.yml + .env to /data/stacks/{name}/
    Converts relative volume paths to absolute paths.

    When *dry_run* is True, no file is written or copied: the function only
    performs path conversion and returns a preview of the converted compose
    file along with the list of conversions and warnings.

    Returns: { success: bool, name: str, conversions: list, warnings: list,
               preview?: str }
    """
    import re as _re
    from datetime import date

    source = Path(source_path).resolve()
    if not source.exists():
        return {"success": False, "error": f"Source path '{source_path}' does not exist"}

    compose_src = source / 'docker-compose.yml'
    if not compose_src.exists():
        # Try compose.yaml
        compose_src = source / 'compose.yaml'
        if not compose_src.exists():
            return {"success": False, "error": "No docker-compose.yml found in source directory"}

    # Determine stack name
    if not stack_name:
        stack_name = source.name

    # Validate stack name
    try:
        validate_stack_name(stack_name)
    except ValueError:
        return {"success": False, "error": f"Invalid stack name: {stack_name}"}

    # Target directory
    stacks_dir = Path(get_data_dir()) / 'stacks'
    target = stacks_dir / stack_name

    # In dry-run mode, do not check for an existing target folder: the user
    # might want to preview even if a folder with the same name already
    # exists (the real import will fail then).
    if not dry_run and target.exists():
        return {"success": False, "error": f"Stack '{stack_name}' already exists in Docky"}

    # Read the compose file
    compose_content = compose_src.read_text(encoding='utf-8')

    # Convert relative paths to absolute
    conversions = []
    warnings = []

    lines = compose_content.split('\n')
    converted_lines = []

    # Get named volumes to avoid converting them
    named_volumes = set()
    in_volumes_section = False
    for line in lines:
        stripped = line.strip()
        if stripped == 'volumes:' and not line.startswith(' '):
            in_volumes_section = True
            continue
        if in_volumes_section:
            if line and not line.startswith(' ') and not line.startswith('#'):
                in_volumes_section = False
            elif stripped and not stripped.startswith('-'):
                # Named volume: volumename:  (a YAML key ending with ':')
                key_part = stripped.split('#')[0].strip()
                if key_part.endswith(':'):
                    vol_name = key_part.rstrip(':').strip()
                    if vol_name:
                        named_volumes.add(vol_name)

    # Track the current YAML path to detect which section we're in
    indent_levels = [0]  # stack of indent levels
    yaml_path: list[str] = []  # e.g. ["services", "n8n", "volumes"]

    for line in lines:
        stripped = line.strip()

        # Skip comments and empty lines
        if stripped.startswith('#') or not stripped:
            converted_lines.append(line)
            continue

        indent = len(line) - len(line.lstrip())

        # --- Update YAML path based on indentation ---
        # If we encounter a YAML key (line ending with ':' not starting with '-'),
        # update the indent stack and yaml_path accordingly.
        if stripped.endswith(':') and not stripped.startswith('- '):
            key = stripped.rstrip(':').strip().split('#')[0].strip()
            if key:
                # Pop indent levels deeper than current line
                while len(indent_levels) > 1 and indent_levels[-1] > indent:
                    indent_levels.pop()
                    if yaml_path:
                        yaml_path.pop()

                if indent > indent_levels[-1]:
                    # Entering a new nested level -> push
                    indent_levels.append(indent)
                    yaml_path.append(key)
                elif indent == indent_levels[-1]:
                    # Same level as previous key -> replace
                    if yaml_path:
                        yaml_path[-1] = key
                    else:
                        yaml_path.append(key)
                # If indent < indent_levels[-1], the while loop above already
                # popped all deeper levels, and we are now at a shallower level
                # which means it's a totally different branch (e.g. top-level
                # volumes: after services:). In that case, replace the last key
                # if we're at the same indent as the remaining stack top.
                elif indent == (indent_levels[-1] if indent_levels else 0):
                    if yaml_path:
                        yaml_path[-1] = key
                    else:
                        yaml_path.append(key)

        # Determine if we are inside a service-level "volumes:" section
        # (not top-level volumes which are named volume declarations).
        is_in_volumes = (
            'volumes' in yaml_path
            and yaml_path.index('volumes') > 0
        )

        # Look for volume mounts: - source:target or - source:target:ro
        if stripped.startswith('- '):
            if not is_in_volumes:
                # Not in a volumes section - pass through unchanged
                converted_lines.append(line)
                continue

            vol_part = stripped[2:].strip()
            # Split by : but be careful of Windows paths (not relevant here)
            parts = vol_part.split(':')
            if len(parts) >= 2:
                source_vol = parts[0].strip()

                # Skip if it's a named volume
                if source_vol in named_volumes:
                    converted_lines.append(line)
                    continue

                # Skip if it's already absolute path
                if source_vol.startswith('/'):
                    converted_lines.append(line)
                    continue

                # Skip if it looks like a variable ${...}
                if '${' in source_vol:
                    warnings.append(f"Variable in volume path: {source_vol} - check manually")
                    converted_lines.append(line)
                    continue

                # Skip if it's a relative path that goes up (../)
                if source_vol.startswith('../'):
                    warnings.append(f"Parent directory path: {source_vol} - check manually")
                    converted_lines.append(line)
                    continue

                # Convert: ./something or something → /abs/path/something
                if source_vol.startswith('./'):
                    source_vol = source_vol[2:]

                # Resolve relative to source directory
                abs_path = str((source / source_vol).resolve())

                # Replace in the line
                new_line = line.replace(source_vol if not parts[0].strip().startswith('./') else './' + source_vol, abs_path)
                if new_line == line:
                    # Fallback: replace the whole source part
                    indent_str = line[:len(line) - len(line.lstrip())]
                    rest = ':'.join(parts[1:])
                    new_line = f"{indent_str}- {abs_path}:{rest}"

                conversions.append(f"{parts[0].strip()} → {abs_path}")
                converted_lines.append(new_line)
                continue

        converted_lines.append(line)

    converted_compose = '\n'.join(converted_lines)

    # Add Docky metadata at the top
    today = date.today().isoformat()
    metadata = f"""# ============================================
# Docky Stack Metadata
# @name: {stack_name}
# @category: imported
# @description: Imported from {source}
# @source: 
# @hardware: 
# @ports: 
# @created: {today}
# @updated: {today}
# ============================================

"""

    # In dry-run mode, do not write or copy anything: just return the
    # preview of the converted compose file.
    if dry_run:
        return {
            "success": True,
            "name": stack_name,
            "conversions": conversions,
            "warnings": warnings,
            "preview": metadata + converted_compose,
        }

    # Create target directory
    target.mkdir(parents=True, exist_ok=False)

    # Write the compose file with metadata
    (target / 'docker-compose.yml').write_text(metadata + converted_compose, encoding='utf-8')

    # Copy .env if exists
    env_src = source / '.env'
    if env_src.exists():
        shutil.copy2(str(env_src), str(target / '.env'))

    # Copy other config files (not docker-compose.yml, not .env, not .git)
    for item in source.iterdir():
        if item.is_file() and item.name not in ['docker-compose.yml', 'compose.yaml', '.env', '.gitignore', '.git']:
            # Only copy config files (yml, yaml, conf, json, txt, sh, env)
            if item.suffix in ['.yml', '.yaml', '.conf', '.json', '.txt', '.sh', '.env', '.ini', '.cfg']:
                shutil.copy2(str(item), str(target / item.name))

    return {
        "success": True,
        "name": stack_name,
        "path": str(target),
        "conversions": conversions,
        "warnings": warnings
    }


async def deploy_stack(name: str) -> Dict[str, Any]:
    """Deploy a stack: ``docker compose down`` then ``docker compose up -d``.

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
        output_parts.append("--- docker compose up -d ---\n" + up_result["output"])
    if up_result.get("error"):
        output_parts.append("--- docker compose up -d (stderr) ---\n" + up_result["error"])
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
    target = _stack_file_path(stack_name, filename)
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
    subprocess.run(["git", "add", str(stack_path)], cwd=str(stacks_dir), capture_output=True)

    # Commit
    from datetime import datetime
    msg = message or f"Save {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    subprocess.run(["git", "commit", "-m", msg, "--allow-empty"], cwd=str(stacks_dir), capture_output=True)


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
    """Keep only the last N versions, squash older ones.

    Uses git reset --soft to squash commits beyond the limit into one.
    Only affects commits that touch the specific stack path.
    """
    stacks_dir = Path(get_data_dir()) / 'stacks'
    git_dir = stacks_dir / '.git'
    if not git_dir.exists():
        return

    # Count commits for this stack
    count_result = subprocess.run(
        ["git", "rev-list", "--count", "HEAD", "--", str(stacks_dir / stack_name)],
        cwd=str(stacks_dir), capture_output=True, text=True
    )
    if count_result.returncode != 0:
        return

    try:
        count = int(count_result.stdout.strip())
    except ValueError:
        return

    if count <= max_versions:
        return  # Nothing to clean up

    # Simple approach: squash old commits
    # We use git reset --soft to squash everything after the Nth commit
    # This keeps the last max_versions commits as-is
    try:
        # Get the hash of the (count - max_versions + 1)th commit from the end
        # This is the first commit we want to KEEP (everything before gets squashed)
        keep_result = subprocess.run(
            ["git", "log", "--oneline", "--", str(stacks_dir / stack_name)],
            cwd=str(stacks_dir), capture_output=True, text=True
        )
        if keep_result.returncode != 0:
            return

        lines = keep_result.stdout.strip().split('\n')
        if len(lines) <= max_versions:
            return

        # The hash of the commit to start squashing from
        # We keep the last max_versions commits
        squash_after = lines[max_versions - 1].split()[0]  # First commit to KEEP

        # Reset soft to this commit (keeps working tree intact)
        # Then recommit with a "squashed" message
        subprocess.run(
            ["git", "reset", "--soft", squash_after],
            cwd=str(stacks_dir), capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Historique antérieur compressé", "--allow-empty"],
            cwd=str(stacks_dir), capture_output=True
        )
        logger.info("Cleaned up history for stack '%s': kept %d versions", stack_name, max_versions)
    except Exception as e:
        logger.warning("Failed to cleanup history for '%s': %s", stack_name, e)


def get_history_settings() -> dict:
    """Get history retention settings from settings.yaml."""
    import yaml
    settings_path = Path(get_data_dir()) / 'settings.yaml'
    if settings_path.exists():
        with open(settings_path) as f:
            settings = yaml.safe_load(f) or {}
        return settings.get('history_retention', {'max_versions': 50})
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


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------

def get_used_ports() -> List[Dict[str, Any]]:
    """Scan for ports in use on the host.

    Combines Docker SDK port mappings with a system scan (``ss`` or
    ``/proc/net/tcp`` / ``/proc/net/tcp6``).
    """
    ports: Dict[str, Dict[str, Any]] = {}

    # 1. Docker port mappings
    try:
        containers = list_containers(all=True)
        for c in containers:
            for p in c.get("ports", []):
                host_port = p.get("host_port", "")
                if host_port:
                    key = host_port
                    if key not in ports:
                        ports[key] = {
                            "port": host_port,
                            "source": "docker",
                            "container": c["name"],
                            "stack": c.get("stack", ""),
                        }
                    else:
                        ports[key]["container"] = c["name"]
                        ports[key]["stack"] = c.get("stack", "")
    except DockerException:
        pass

    # 2. System scan via ss (preferred) or netstat
    sys_ports = _scan_system_ports()
    for port in sys_ports:
        key = str(port)
        if key not in ports:
            ports[key] = {
                "port": key,
                "source": "system",
                "container": "",
                "stack": "",
            }

    return sorted(ports.values(), key=lambda x: int(x["port"]) if x["port"].isdigit() else 0)


def _scan_system_ports() -> List[int]:
    """Scan listening ports on the host using ss, netstat or /proc."""
    try:
        result = subprocess.run(
            ["ss", "-tlnH"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout:
            return _parse_ss_output(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    try:
        result = subprocess.run(
            ["netstat", "-tln"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout:
            return _parse_netstat_output(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return _parse_proc_net()


def _parse_ss_output(output: str) -> List[int]:
    """Parse ``ss -tlnH`` output and return listening ports."""
    ports: set[int] = set()
    for line in output.strip().splitlines():
        parts = line.split()
        if len(parts) >= 4:
            local = parts[3]
            if ":" in local:
                port_str = local.rsplit(":", 1)[-1]
                if port_str.isdigit():
                    ports.add(int(port_str))
    return sorted(ports)


def _parse_netstat_output(output: str) -> List[int]:
    """Parse ``netstat -tln`` output and return listening ports."""
    ports: set[int] = set()
    for line in output.strip().splitlines():
        if "LISTEN" not in line:
            continue
        parts = line.split()
        if len(parts) >= 4:
            local = parts[3]
            if ":" in local:
                port_str = local.rsplit(":", 1)[-1]
                if port_str.isdigit():
                    ports.add(int(port_str))
    return sorted(ports)


def _parse_proc_net() -> List[int]:
    """Parse ``/proc/net/tcp`` and ``/proc/net/tcp6`` for listening ports."""
    ports: set[int] = set()
    for path in ["/proc/net/tcp", "/proc/net/tcp6"]:
        try:
            with open(path, "r") as f:
                lines = f.readlines()
        except (OSError, IOError):
            continue
        for line in lines[1:]:
            parts = line.split()
            if len(parts) < 4:
                continue
            local_addr = parts[1]
            state = parts[3]
            if state != "0A":
                continue
            if ":" in local_addr:
                port_hex = local_addr.rsplit(":", 1)[-1]
                try:
                    port = int(port_hex, 16)
                    ports.add(port)
                except ValueError:
                    continue
    return sorted(ports)


# ---------------------------------------------------------------------------
# Update check
# ---------------------------------------------------------------------------

def check_image_update(container_id: str) -> Dict[str, Any]:
    """Check if a newer image is available on the registry for a container.

    Compares the local image digest with the remote registry digest.
    Returns a dict with ``update_available`` (bool), ``local_digest`` and
    ``remote_digest`` (if available).
    """
    try:
        client = get_docker_client()
        c = client.containers.get(container_id)
        image = c.image
        image_name = image.tags[0] if image.tags else None
        local_id = image.id

        if not image_name:
            return {
                "update_available": False,
                "local_digest": local_id,
                "remote_digest": None,
                "error": "No image tag found",
            }

        local_digest = None
        try:
            digest_list = image.attrs.get("RepoDigests", [])
            if digest_list:
                local_digest = digest_list[0]
        except Exception:
            pass

        try:
            remote_image = client.images.pull(image_name)
            remote_digest = remote_image.id
            update_available = remote_digest != local_id
            return {
                "update_available": update_available,
                "local_digest": local_digest or local_id,
                "remote_digest": remote_digest,
                "image": image_name,
            }
        except (DockerException, APIError) as e:
            return {
                "update_available": False,
                "local_digest": local_digest or local_id,
                "remote_digest": None,
                "image": image_name,
                "error": str(e),
            }
    except (NotFound, DockerException, APIError) as e:
        return {
            "update_available": False,
            "local_digest": None,
            "remote_digest": None,
            "error": str(e),
        }