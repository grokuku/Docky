"""Scan des ports en écoute (extrait de docker_manager).

Combine les mappings de ports Docker avec un scan système (``ss`` / ``netstat``
/ ``/proc/net``). Fonctions ré-exportées dans le namespace
``agent.docker_manager`` (façade). ``get_used_ports`` appelle
``list_containers`` (qui reste dans docker_manager) via ``_dm()`` au moment de
l'appel pour rester compatible avec les monkeypatchs des tests.
"""

import logging
import subprocess
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

from docker.errors import DockerException


def _dm():
    """Résolution tardive du namespace agent.docker_manager (évite tout cycle)."""
    from agent import docker_manager
    return docker_manager


def get_used_ports() -> List[Dict[str, Any]]:
    """Scan for ports in use on the host.

    Combines Docker SDK port mappings with a system scan (``ss`` or
    ``/proc/net/tcp`` / ``/proc/net/tcp6``).
    """
    ports: Dict[str, Dict[str, Any]] = {}

    # 1. Docker port mappings
    try:
        containers = _dm().list_containers(all=True)
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
    except DockerException as exc:
        logger.warning("Docker port scan failed; falling back to system scan: %s", exc)

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
