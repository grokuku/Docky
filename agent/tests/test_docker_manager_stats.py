"""Unit tests for agent.docker_manager container stats (LOT B).

Covers the memory formula (``usage - page cache``, like ``docker stats``),
the multi-core CPU percentage + ``cpu_count`` exposure, network aggregation,
the ``found``/``error`` surface used by the integration façade and the bounded
batch helper. Everything is hermetic (no Docker daemon).
"""

from unittest import mock

import docker

from agent import docker_manager as dm


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeImage:
    def __init__(self):
        self.tags = ["nginx:latest"]
        self.id = "sha256:image"


class _StatsContainer:
    """Minimal docker-py container exposing ``stats()`` + the attrs used by
    ``_container_to_dict`` (state/health/name/image)."""

    def __init__(self, cid="abcdef123456", name="web", status="running", stats=None, health=None):
        self.id = cid
        self.short_id = cid[:12]
        self.name = name
        self.status = status
        self.image = _FakeImage()
        state = {}
        if health is not None:
            state["Health"] = {"Status": health}
        self.attrs = {"Config": {"Labels": {}}, "State": state, "Created": ""}
        self.ports = {}
        self._stats = stats if stats is not None else {}

    def stats(self, stream=False):
        return self._stats


class _FakeContainersManager:
    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, ref):
        if ref in self._mapping:
            return self._mapping[ref]
        raise docker.errors.NotFound(f"container {ref} not found")


class _FakeDockerClient:
    def __init__(self, mapping):
        self.containers = _FakeContainersManager(mapping)


def _patch_client(monkeypatch, mapping):
    client = _FakeDockerClient(mapping)
    monkeypatch.setattr(dm, "get_docker_client", lambda: client)
    return client


def _cpu_snapshot(cpu_delta=100, system_delta=100, online_cpus=None, percpu=None):
    cpu_stats = {
        "cpu_usage": {"total_usage": cpu_delta + 100, "percpu_usage": percpu or []},
        "system_cpu_usage": system_delta + 900,
    }
    precpu_stats = {
        "cpu_usage": {"total_usage": 100},
        "system_cpu_usage": 900,
    }
    if online_cpus is not None:
        cpu_stats["online_cpus"] = online_cpus
    return {"cpu_stats": cpu_stats, "precpu_stats": precpu_stats}


def _full_stats(mem_stats=None, cpu_stats=None, networks=None):
    return {
        "cpu_stats": (cpu_stats or {}).get("cpu_stats", {}),
        "precpu_stats": (cpu_stats or {}).get("precpu_stats", {}),
        "memory_stats": mem_stats or {},
        "networks": networks or {},
    }


# ---------------------------------------------------------------------------
# Memory formula (usage - cache, like `docker stats`)
# ---------------------------------------------------------------------------

def test_memory_cgroup_v2_subtracts_inactive_file(monkeypatch):
    container = _StatsContainer(
        stats=_full_stats(mem_stats={"usage": 1000, "limit": 10000, "stats": {"inactive_file": 400}})
    )
    _patch_client(monkeypatch, {"web": container})

    stats = dm.get_container_stats("web")

    assert stats["mem_usage"] == 600
    assert stats["mem_cache"] == 400
    assert stats["mem_limit"] == 10000
    assert stats["mem_percent"] == 6.0


def test_memory_cgroup_v1_subtracts_total_inactive_file(monkeypatch):
    container = _StatsContainer(
        stats=_full_stats(mem_stats={"usage": 1000, "limit": 2000, "stats": {"total_inactive_file": 250}})
    )
    _patch_client(monkeypatch, {"web": container})

    stats = dm.get_container_stats("web")

    assert stats["mem_usage"] == 750
    assert stats["mem_cache"] == 250
    assert stats["mem_percent"] == 37.5


def test_memory_without_cache_field_uses_raw_usage(monkeypatch):
    container = _StatsContainer(stats=_full_stats(mem_stats={"usage": 1000, "limit": 10000, "stats": {}}))
    _patch_client(monkeypatch, {"web": container})

    stats = dm.get_container_stats("web")

    assert stats["mem_usage"] == 1000
    assert stats["mem_cache"] == 0


def test_memory_cache_larger_than_usage_is_clamped(monkeypatch):
    container = _StatsContainer(
        stats=_full_stats(mem_stats={"usage": 1000, "limit": 10000, "stats": {"inactive_file": 5000}})
    )
    _patch_client(monkeypatch, {"web": container})

    stats = dm.get_container_stats("web")

    assert stats["mem_usage"] == 1000
    assert stats["mem_cache"] == 0


# ---------------------------------------------------------------------------
# CPU (multi-core raw + cpu_count)
# ---------------------------------------------------------------------------

def test_cpu_multi_core_percentage_and_cpu_count(monkeypatch):
    container = _StatsContainer(stats=_full_stats(cpu_stats=_cpu_snapshot(online_cpus=4)))
    _patch_client(monkeypatch, {"web": container})

    stats = dm.get_container_stats("web")

    # (100/100) * 4 * 100 = 400 % (multi-core, not yet host-normalised)
    assert stats["cpu_percent"] == 400.0
    assert stats["cpu_count"] == 4


def test_cpu_count_falls_back_to_percpu_usage(monkeypatch):
    container = _StatsContainer(
        stats=_full_stats(cpu_stats=_cpu_snapshot(online_cpus=None, percpu=[1, 2]))
    )
    _patch_client(monkeypatch, {"web": container})

    stats = dm.get_container_stats("web")

    assert stats["cpu_count"] == 2
    assert stats["cpu_percent"] == 200.0


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

def test_network_counters_are_aggregated(monkeypatch):
    container = _StatsContainer(
        stats=_full_stats(networks={
            "eth0": {"rx_bytes": 1000, "tx_bytes": 2000},
            "eth1": {"rx_bytes": 500, "tx_bytes": 300},
        })
    )
    _patch_client(monkeypatch, {"web": container})

    stats = dm.get_container_stats("web")

    assert stats["network_rx"] == 1500
    assert stats["network_tx"] == 2300


# ---------------------------------------------------------------------------
# Missing container -> zeroed stats
# ---------------------------------------------------------------------------

def test_get_container_stats_missing_returns_zeroed(monkeypatch):
    _patch_client(monkeypatch, {})

    stats = dm.get_container_stats("ghost")

    assert stats == {
        "cpu_percent": 0.0,
        "cpu_count": 1,
        "mem_usage": 0,
        "mem_limit": 0,
        "mem_percent": 0.0,
        "mem_cache": 0,
        "network_rx": 0,
        "network_tx": 0,
    }


def test_get_container_stats_result_found(monkeypatch):
    container = _StatsContainer(
        cid="abcdef123456", name="web", status="running", health="healthy",
        stats=_full_stats(
            mem_stats={"usage": 1000, "limit": 10000, "stats": {"inactive_file": 400}},
            cpu_stats=_cpu_snapshot(online_cpus=2),
        ),
    )
    _patch_client(monkeypatch, {"web": container})

    result = dm.get_container_stats_result("web")

    assert result["found"] is True
    assert result["id"] == "abcdef123456"
    assert result["name"] == "web"
    assert result["state"] == "running"
    assert result["health"] == "healthy"
    assert result["mem_usage"] == 600
    assert result["mem_cache"] == 400
    assert result["cpu_count"] == 2


def test_get_container_stats_result_missing(monkeypatch):
    _patch_client(monkeypatch, {})

    result = dm.get_container_stats_result("ghost")

    assert result["found"] is False
    assert result["state"] == "unknown"
    assert result["error"] == "Container not found"
    assert result["mem_usage"] == 0
    assert result["cpu_count"] == 1


# ---------------------------------------------------------------------------
# Batch helper
# ---------------------------------------------------------------------------

def test_get_containers_stats_preserves_order_and_partial_errors(monkeypatch):
    container = _StatsContainer(
        cid="abcdef123456", name="web",
        stats=_full_stats(mem_stats={"usage": 1000, "limit": 2000, "stats": {"inactive_file": 0}}),
    )
    _patch_client(monkeypatch, {"web": container, "abcdef123456": container})

    results = dm.get_containers_stats(["web", "missing", "abcdef123456"])

    assert [r["found"] for r in results] == [True, False, True]
    assert results[0]["name"] == "web"
    assert results[1]["error"] == "Container not found"
    assert results[2]["id"] == "abcdef123456"


def test_get_containers_stats_empty():
    assert dm.get_containers_stats([]) == []
