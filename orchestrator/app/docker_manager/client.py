"""Docker SDK client utilities for Docky.

Connects to the Docker daemon via the socket mounted at
``/var/run/docker.sock`` (or as configured by the environment).
"""

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import docker
from docker.errors import DockerException, NotFound, APIError

from app.config import get_data_dir


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
    # Fallback: let the SDK figure it out
    return docker.from_env()


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------

def _container_to_dict(c) -> Dict[str, Any]:
    """Convert a Docker container object to a serialisable dict."""
    labels = c.attrs.get("Config", {}).get("Labels", {}) or {}
    state = c.attrs.get("State", {})
    # Ports
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

    # Determine a simple status string
    status_label = c.status  # e.g. "running", "exited"
    health = state.get("Health", {}).get("Status") if isinstance(state.get("Health"), dict) else None

    return {
        "id": c.short_id,
        "name": c.name.lstrip("/") if c.name else "",
        "image": c.image.tags[0] if c.image.tags else str(c.image.id),
        "image_id": c.image.id,
        "status": status_label,
        "state": status_label,
        "health": health,
        "ports": port_list,
        "stack": labels.get("com.docker.compose.project", ""),
        "labels": labels,
        "created": c.attrs.get("Created", ""),
    }


def list_containers(all: bool = True) -> List[Dict[str, Any]]:
    """Return a list of containers with their key properties.

    Each entry contains: ``id``, ``name``, ``image``, ``status``,
    ``state``, ``ports`` and ``stack`` (the compose project label if
    available).
    """
    try:
        client = get_docker_client()
        containers = client.containers.list(all=all)
    except DockerException:
        return []

    result: List[Dict[str, Any]] = []
    for c in containers:
        result.append(_container_to_dict(c))
    return result


def get_container(container_id: str) -> Optional[Dict[str, Any]]:
    """Return details for a single container, or ``None`` if not found."""
    try:
        client = get_docker_client()
        c = client.containers.get(container_id)
    except (NotFound, DockerException):
        return None
    return _container_to_dict(c)


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


def get_container_logs(container_id: str, tail: int = 100) -> List[str]:
    """Return the last ``tail`` log lines of a container."""
    try:
        client = get_docker_client()
        c = client.containers.get(container_id)
        raw = c.logs(stdout=True, stderr=True, tail=tail, timestamps=False)
    except (NotFound, DockerException, APIError):
        return []
    # Decode and split into lines
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    lines = raw.splitlines()
    return lines


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

    # CPU
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
        # Fallback: count per-cpu usage entries
        per_cpu = cpu_stats.get("cpu_usage", {}).get("percpu_usage", [])
        cpu_count = len(per_cpu) if per_cpu else 1

    if system_delta > 0 and cpu_delta > 0:
        cpu_percent = (cpu_delta / system_delta) * cpu_count * 100.0

    # Memory
    mem_stats = stats.get("memory_stats", {})
    mem_usage = mem_stats.get("usage", 0)
    mem_limit = mem_stats.get("limit", 0)
    mem_percent = 0.0
    if mem_limit > 0:
        mem_percent = (mem_usage / mem_limit) * 100.0

    # Network
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


# ---------------------------------------------------------------------------
# Stacks
# ---------------------------------------------------------------------------

def get_stacks_dir() -> Path:
    """Return the path to the stacks directory inside the data dir."""
    return get_data_dir() / "stacks"


def list_stacks() -> List[Dict[str, Any]]:
    """Scan the stacks directory for folders containing a docker-compose.yml.

    Returns a list of dicts with ``name``, ``path``, ``has_compose`` and
    ``has_env``.
    """
    stacks_dir = get_stacks_dir()
    if not stacks_dir.exists():
        return []

    result: List[Dict[str, Any]] = []
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
            }
        )
    return result


def get_stack_containers(stack_name: str) -> List[Dict[str, Any]]:
    """Return all containers belonging to a compose stack."""
    containers = list_containers(all=True)
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


def _compose_file_arg(stack_path: Path) -> str:
    """Return the -f argument for docker compose, detecting the compose file."""
    for name in ["docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"]:
        candidate = stack_path / name
        if candidate.exists():
            return f"-f {candidate}"
    return ""


def _run_compose(stack_name: str, command: str) -> Dict[str, Any]:
    """Run a docker compose subcommand for a stack."""
    stack_path = get_stacks_dir() / stack_name
    if not stack_path.exists():
        return {"success": False, "error": f"Stack '{stack_name}' not found"}

    file_arg = _compose_file_arg(stack_path)
    if not file_arg:
        return {"success": False, "error": "No compose file found in stack directory"}

    full_cmd = f"docker compose {file_arg} {command}"
    try:
        result = subprocess.run(
            full_cmd,
            shell=True,
            capture_output=True,
            text=True,
            cwd=str(stack_path),
            timeout=120,
        )
        if result.returncode == 0:
            return {"success": True, "output": result.stdout, "command": full_cmd}
        else:
            return {"success": False, "error": result.stderr or result.stdout, "command": full_cmd}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Command timed out", "command": full_cmd}
    except Exception as e:
        return {"success": False, "error": str(e), "command": full_cmd}


def compose_up(stack_name: str) -> Dict[str, Any]:
    """Run ``docker compose up -d`` for the given stack."""
    return _run_compose(stack_name, "up -d")


def compose_down(stack_name: str) -> Dict[str, Any]:
    """Run ``docker compose down`` for the given stack."""
    return _run_compose(stack_name, "down")


def compose_stop(stack_name: str) -> Dict[str, Any]:
    """Run ``docker compose stop`` for the given stack."""
    return _run_compose(stack_name, "stop")


def compose_restart(stack_name: str) -> Dict[str, Any]:
    """Run ``docker compose restart`` for the given stack."""
    return _run_compose(stack_name, "restart")


# ---------------------------------------------------------------------------
# Stack file management (Phase 3)
# ---------------------------------------------------------------------------

_STACK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9.][A-Za-z0-9_.-]*$")


def validate_stack_name(name: str) -> str:
    """Return the stack name if valid, raise ValueError otherwise.

    Allowed: alphanumerics, dots, hyphens, underscores. Must start with an
    alphanumeric character. No path separators, no ``..``.
    """
    if not name or not _STACK_NAME_RE.match(name):
        raise ValueError(f"Invalid stack name: {name!r}")
    if ".." in name or "/" in name or "\\" in name:
        raise ValueError(f"Invalid stack name: {name!r}")
    return name


# Backwards-compatible alias
_validate_stack_name = validate_stack_name


def validate_filename(filename: str) -> str:
    """Validate a filename within a stack directory.

    Only the base name is allowed (no path separators). Hidden files
    (starting with a dot) like ``.env`` are accepted. Sub-paths are not
    allowed in this phase (single-level directory).
    """
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


# Backwards-compatible alias
_validate_filename = validate_filename


def safe_join(stack_name: str, filename: str) -> Path:
    """Join *filename* to the stack directory and verify the resolved path
    stays inside ``/data/stacks/{stack_name}/``.

    Raises ``ValueError`` if the stack name or filename is invalid, or if a
    path traversal attempt is detected. Returns the resolved ``Path``.
    """
    validate_filename(filename)
    base = _stack_dir(stack_name)
    target = (base / filename).resolve()
    # Ensure target is inside base
    if base != target and base not in target.parents:
        raise ValueError("Path traversal detected")
    return target


# Backwards-compatible alias
_stack_file_path = safe_join


def _stack_dir(name: str) -> Path:
    """Return the resolved path to a stack directory."""
    validate_stack_name(name)
    return (get_stacks_dir() / name).resolve()


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
    return {"name": name, "path": str(base)}


def delete_stack(name: str) -> Dict[str, Any]:
    """Delete a stack directory entirely."""
    base = _stack_dir(name)
    if not base.exists():
        raise FileNotFoundError(f"Stack '{name}' not found")
    # Safety: ensure base is inside stacks dir
    stacks_dir = get_stacks_dir().resolve()
    if base != stacks_dir and stacks_dir not in base.parents:
        raise ValueError("Refusing to delete: path outside stacks directory")
    shutil.rmtree(base)
    return {"name": name, "deleted": True}


def deploy_stack(name: str) -> Dict[str, Any]:
    """Deploy a stack: ``docker compose down`` then ``docker compose up -d``.

    Returns a dict with ``success``, ``output`` and ``error``.
    Raises ``FileNotFoundError`` if the stack directory does not exist.
    """
    base = _stack_dir(name)
    if not base.exists():
        raise FileNotFoundError(f"Stack '{name}' not found")
    down_result = compose_down(name)
    up_result = compose_up(name)
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
        # Strip leading 0o if present, parse as octal
        mode_str = mode.strip()
        if mode_str.startswith("0o") or mode_str.startswith("0O"):
            mode_int = int(mode_str, 8)
        else:
            mode_int = int(mode_str, 8)
    else:
        mode_int = int(mode)
    os.chmod(target, mode_int)
    # Return the new mode in octal string
    new_mode = oct(target.stat().st_mode & 0o777)
    return {"name": filename, "mode": new_mode}


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
                        # Merge info
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
    # Try ss first
    try:
        result = subprocess.run(
            ["ss", "-tlnH"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout:
            return _parse_ss_output(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Try netstat
    try:
        result = subprocess.run(
            ["netstat", "-tln"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout:
            return _parse_netstat_output(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback: /proc/net/tcp (if running on Linux host)
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
        for line in lines[1:]:  # skip header
            parts = line.split()
            if len(parts) < 4:
                continue
            local_addr = parts[1]
            state = parts[3]
            # state 0A = LISTEN
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

        # Get local repo digests
        local_digest = None
        try:
            digest_list = image.attrs.get("RepoDigests", [])
            if digest_list:
                local_digest = digest_list[0]
        except Exception:
            pass

        # Try to pull and compare
        try:
            remote_image = client.images.pull(image_name)
            remote_digest = remote_image.id
            # Compare IDs
            update_available = remote_digest != local_id
            return {
                "update_available": update_available,
                "local_digest": local_digest or local_id,
                "remote_digest": remote_digest,
                "image": image_name,
            }
        except (DockerException, APIError) as e:
            # If pull fails, maybe registry not accessible / image is local
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


# ===== Phase 3: Stack file management =====

def validate_stack_name(name: str) -> bool:
    """Validate that a stack name is safe (alphanumeric + hyphens/underscores only)."""
    if not name:
        return False
    import re
    return bool(re.match(r'^[a-zA-Z0-9][a-zA-Z0-9_-]*$', name))

def validate_filename(filename: str) -> bool:
    """Validate that a filename has no path traversal."""
    if not filename or '..' in filename or '/' in filename:
        return False
    # Allow filenames with dots (like docker-compose.yml, .env)
    import re
    return bool(re.match(r'^[a-zA-Z0-9._-]+$', filename))

def get_stack_dir(stack_name: str) -> Path:
    """Get the directory path for a stack."""
    from app.config import get_data_dir
    return Path(get_data_dir()) / 'stacks' / stack_name

def safe_join(stack_name: str, filename: str) -> Path:
    """Safely join stack dir with filename, ensuring result stays within stack dir."""
    stack_dir = get_stack_dir(stack_name).resolve()
    filepath = (stack_dir / filename).resolve()
    # Ensure the resolved path is within the stack directory
    if not str(filepath).startswith(str(stack_dir)):
        raise ValueError("Path traversal detected")
    return filepath

def get_stack_files(stack_name: str) -> list:
    """List files in a stack directory (1 level deep)."""
    stack_dir = get_stack_dir(stack_name)
    if not stack_dir.exists():
        raise FileNotFoundError(f"Stack '{stack_name}' not found")
    return [f.name for f in stack_dir.iterdir() if f.is_file()]

def get_stack_file(stack_name: str, filename: str) -> str:
    """Read the content of a file in a stack."""
    filepath = safe_join(stack_name, filename)
    if not filepath.exists():
        raise FileNotFoundError(f"File '{filename}' not found in stack '{stack_name}'")
    return filepath.read_text(encoding='utf-8')

def save_stack_file(stack_name: str, filename: str, content: str) -> None:
    """Save content to a file in a stack (creates if not exists)."""
    stack_dir = get_stack_dir(stack_name)
    if not stack_dir.exists():
        raise FileNotFoundError(f"Stack '{stack_name}' not found")
    filepath = safe_join(stack_name, filename)
    filepath.write_text(content, encoding='utf-8')

def create_stack(name: str, compose_content: str, env_content: str = None) -> str:
    """Create a new stack directory with docker-compose.yml and optional .env."""
    if not validate_stack_name(name):
        raise ValueError("Invalid stack name")
    stack_dir = get_stack_dir(name)
    if stack_dir.exists():
        raise FileExistsError(f"Stack '{name}' already exists")
    stack_dir.mkdir(parents=True)
    (stack_dir / 'docker-compose.yml').write_text(compose_content, encoding='utf-8')
    if env_content is not None:
        (stack_dir / '.env').write_text(env_content, encoding='utf-8')
    return str(stack_dir)

def delete_stack(name: str) -> None:
    """Delete a stack and all its files."""
    if not validate_stack_name(name):
        raise ValueError("Invalid stack name")
    stack_dir = get_stack_dir(name)
    if not stack_dir.exists():
        raise FileNotFoundError(f"Stack '{name}' not found")
    import shutil
    shutil.rmtree(stack_dir)

def deploy_stack(name: str) -> dict:
    """Deploy a stack: docker compose down then docker compose up -d."""
    stack_dir = get_stack_dir(name)
    if not stack_dir.exists():
        raise FileNotFoundError(f"Stack '{name}' not found")
    import subprocess
    result_down = subprocess.run(
        ['docker', 'compose', 'down'],
        cwd=str(stack_dir), capture_output=True, text=True, timeout=60
    )
    result_up = subprocess.run(
        ['docker', 'compose', 'up', '-d'],
        cwd=str(stack_dir), capture_output=True, text=True, timeout=120
    )
    success = result_up.returncode == 0
    output = result_down.stdout + result_down.stderr + result_up.stdout + result_up.stderr
    return {"success": success, "output": output}

def set_file_permissions(stack_name: str, filename: str, mode: str) -> None:
    """Set file permissions (chmod) on a file in a stack. mode is octal string like '644'."""
    filepath = safe_join(stack_name, filename)
    if not filepath.exists():
        raise FileNotFoundError(f"File '{filename}' not found in stack '{stack_name}'")
    os.chmod(filepath, int(mode, 8))