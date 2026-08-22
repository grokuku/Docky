"""Tests for the agent API-key authentication helpers.

These are pure unit tests: the FastAPI ``Request`` / ``WebSocket`` objects are
replaced by minimal stubs exposing only the attributes the auth code reads.
"""

import pytest

from agent import auth


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

class FakeRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}


class FakeWebSocket:
    def __init__(self, query_params=None, headers=None):
        self.query_params = query_params or {}
        self.headers = headers or {}


# ---------------------------------------------------------------------------
# get_api_key
# ---------------------------------------------------------------------------

def test_get_api_key_from_env(monkeypatch):
    monkeypatch.setenv("DOCKY_AGENT_API_KEY", "env-key")
    assert auth.get_api_key() == "env-key"


def test_get_api_key_default_empty(monkeypatch):
    monkeypatch.delenv("DOCKY_AGENT_API_KEY", raising=False)
    assert auth.get_api_key() == ""


# ---------------------------------------------------------------------------
# verify_api_key
# ---------------------------------------------------------------------------

def test_verify_api_key_bearer_correct(monkeypatch):
    monkeypatch.setenv("DOCKY_AGENT_API_KEY", "test-key")
    assert auth.verify_api_key(FakeRequest({"Authorization": "Bearer test-key"})) is True


def test_verify_api_key_wrong_key(monkeypatch):
    monkeypatch.setenv("DOCKY_AGENT_API_KEY", "test-key")
    assert auth.verify_api_key(FakeRequest({"Authorization": "Bearer wrong"})) is False


def test_verify_api_key_missing_header(monkeypatch):
    monkeypatch.setenv("DOCKY_AGENT_API_KEY", "test-key")
    assert auth.verify_api_key(FakeRequest({})) is False


def test_verify_api_key_empty_header(monkeypatch):
    monkeypatch.setenv("DOCKY_AGENT_API_KEY", "test-key")
    assert auth.verify_api_key(FakeRequest({"Authorization": ""})) is False


def test_verify_api_key_non_bearer(monkeypatch):
    monkeypatch.setenv("DOCKY_AGENT_API_KEY", "test-key")
    assert auth.verify_api_key(FakeRequest({"Authorization": "test-key"})) is False
    assert auth.verify_api_key(FakeRequest({"Authorization": "Basic Zm9vOmJhcg=="})) is False


def test_verify_api_key_no_configured_key(monkeypatch):
    monkeypatch.delenv("DOCKY_AGENT_API_KEY", raising=False)
    assert auth.verify_api_key(FakeRequest({"Authorization": "Bearer test-key"})) is False


# ---------------------------------------------------------------------------
# require_api_key
# ---------------------------------------------------------------------------

def test_require_api_key_returns_none_when_valid(monkeypatch):
    monkeypatch.setenv("DOCKY_AGENT_API_KEY", "test-key")
    assert auth.require_api_key(FakeRequest({"Authorization": "Bearer test-key"})) is None


def test_require_api_key_returns_401(monkeypatch):
    import json

    monkeypatch.setenv("DOCKY_AGENT_API_KEY", "test-key")
    resp = auth.require_api_key(FakeRequest({}))
    assert resp is not None
    assert resp.status_code == 401
    body = json.loads(resp.body.decode("utf-8"))
    assert "error" in body


# ---------------------------------------------------------------------------
# verify_api_key_ws (async)
# ---------------------------------------------------------------------------

async def test_verify_api_key_ws_query_param(monkeypatch):
    monkeypatch.setenv("DOCKY_AGENT_API_KEY", "test-key")
    ws = FakeWebSocket(query_params={"api_key": "test-key"})
    assert await auth.verify_api_key_ws(ws) is True


async def test_verify_api_key_ws_bearer_header(monkeypatch):
    monkeypatch.setenv("DOCKY_AGENT_API_KEY", "test-key")
    ws = FakeWebSocket(headers={"Authorization": "Bearer test-key"})
    assert await auth.verify_api_key_ws(ws) is True


async def test_verify_api_key_ws_query_preferred_over_header(monkeypatch):
    monkeypatch.setenv("DOCKY_AGENT_API_KEY", "test-key")
    ws = FakeWebSocket(
        query_params={"api_key": "test-key"},
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert await auth.verify_api_key_ws(ws) is True


async def test_verify_api_key_ws_wrong(monkeypatch):
    monkeypatch.setenv("DOCKY_AGENT_API_KEY", "test-key")
    ws = FakeWebSocket(
        query_params={"api_key": "wrong"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert await auth.verify_api_key_ws(ws) is False


async def test_verify_api_key_ws_missing(monkeypatch):
    monkeypatch.setenv("DOCKY_AGENT_API_KEY", "test-key")
    assert await auth.verify_api_key_ws(FakeWebSocket()) is False
