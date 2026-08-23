"""Tests for the stack streaming helpers (deploy / down) in compose_stream.

Covers the new deploy semantics (``up -d`` seul, plus de ``down``
systématique) and the new explicit ``down`` action. All tests are hermetic:
the compose resolution and the underlying streaming step are monkeypatched.
"""

from agent.docker import compose_stream


class _FakeDM:
    """Stub standing in for ``agent.docker_manager`` (via ``_dm()``)."""

    def __init__(self, compose_file):
        self.compose_file = compose_file
        self.invalidated = 0

    def _resolve_stack_compose(self, name):
        return (self.compose_file, None)

    def _invalidate_stack_update_cache(self, name):
        self.invalidated += 1


async def _fake_step(name, command, label=None, idle_timeout=120):
    """Yield the label (if any) then a successful result carrying the command."""
    if label:
        yield {"type": compose_stream.STREAM_EVENT_OUTPUT, "line": label}
    yield {
        "type": compose_stream.STREAM_EVENT_RESULT,
        "success": True,
        "output": "",
        "error": "",
        "command": command,
    }


def _install(fake, monkeypatch):
    monkeypatch.setattr(compose_stream, "_dm", lambda: fake)


async def test_stream_deploy_stack_runs_up_only(monkeypatch, tmp_path):
    """Deploy must only run ``up`` — never ``down``."""
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services:\n")
    fake = _FakeDM(str(compose_file))
    _install(fake, monkeypatch)
    monkeypatch.setattr(compose_stream, "_stream_compose_step", _fake_step)

    events = [e async for e in compose_stream.stream_deploy_stack("myapp")]

    result = events[-1]
    assert result["success"] is True
    assert result["command"] == "up -d --remove-orphans"
    # No down command anywhere in the stream.
    joined = "\n".join(e.get("line", "") for e in events)
    assert "down" not in joined
    assert "up -d --remove-orphans" in joined
    assert fake.invalidated == 1


async def test_stream_down_stack_runs_down(monkeypatch, tmp_path):
    """stream_down_stack must emit ``docker compose down``."""
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services:\n")
    fake = _FakeDM(str(compose_file))
    _install(fake, monkeypatch)
    monkeypatch.setattr(compose_stream, "_stream_compose_step", _fake_step)

    events = [e async for e in compose_stream.stream_down_stack("myapp")]

    result = events[-1]
    assert result["success"] is True
    assert result["command"] == "down"
    joined = "\n".join(e.get("line", "") for e in events)
    assert "docker compose down" in joined
