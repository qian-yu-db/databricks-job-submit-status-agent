"""Unit tests for the MCP Streamable-HTTP wire-call path in agent.app.

These tests run fully offline: httpx.Client is mocked so no live server is needed.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock


# ── SSE helpers ──────────────────────────────────────────────────────────────

def _sse(data: dict) -> str:
    return f"event: message\ndata: {json.dumps(data)}\n\n"


def _init_sse() -> str:
    return _sse({
        "jsonrpc": "2.0", "id": 0,
        "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "serverInfo": {"name": "servicenow-mcp", "version": "1.30.0"},
        },
    })


def _tool_sse(caller_id: str = "user@bwater.com") -> str:
    incident = {
        "number": "INC0042001", "sys_id": "stub-42001",
        "caller_id": caller_id, "short_description": "Suspicious logins",
        "state": "New",
    }
    return _sse({
        "jsonrpc": "2.0", "id": 1,
        "result": {
            "content": [{"type": "text", "text": json.dumps(incident)}],
            "isError": False,
        },
    })


class _FakeResp:
    def __init__(self, status_code: int, text: str, headers: dict | None = None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


# ── Fixture: a mock httpx.Client that returns proper MCP SSE ─────────────────

def _make_mock_client(caller_id_slot: list[str | None]):
    """Return a context-manager-compatible mock Client.

    Stores the caller_id seen in tools/call in caller_id_slot[0].
    """

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def post(self, url: str, json: dict | None = None, headers: dict | None = None):
            method = (json or {}).get("method", "")
            if method == "initialize":
                return _FakeResp(200, _init_sse(), {"mcp-session-id": "sess-abc123"})
            if method == "notifications/initialized":
                return _FakeResp(202, "", {})
            if method == "tools/call":
                cid = (json or {}).get("params", {}).get("arguments", {}).get("caller_id", "")
                caller_id_slot[0] = cid
                return _FakeResp(200, _tool_sse(cid), {})
            return _FakeResp(200, "{}", {})

    return _Client()


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_wire_path_sends_caller_id_and_parses_incident(monkeypatch):
    """Wire path: full handshake, caller_id forwarded to MCP, incident returned."""
    import agent.app as app_mod

    monkeypatch.setattr(app_mod, "SERVICENOW_MCP_URL", "https://mock-mcp.example.com")
    monkeypatch.setattr(app_mod, "_mcp_auth_headers", lambda: {"Authorization": "Bearer test"})

    captured: list[str | None] = [None]
    monkeypatch.setattr(app_mod.httpx, "Client", lambda **kw: _make_mock_client(captured))

    result = app_mod._call_servicenow_mcp("Suspicious logins", "details", "ryan@bwater.com")

    assert captured[0] == "ryan@bwater.com", "caller_id must reach the MCP tools/call"
    assert result["number"] == "INC0042001"
    assert result["caller_id"] == "ryan@bwater.com"
    assert result.get("_path") == "wire"


def test_wire_fallback_on_error_uses_stub(monkeypatch):
    """When the wire call raises, fallback to in-process stub with _path='stub'."""
    import agent.app as app_mod

    monkeypatch.setattr(app_mod, "SERVICENOW_MCP_URL", "https://mock-mcp.example.com")
    monkeypatch.setattr(app_mod, "_mcp_auth_headers", lambda: {})

    class _FailClient:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def post(self, *a, **kw):
            raise RuntimeError("simulated network failure")

    monkeypatch.setattr(app_mod.httpx, "Client", lambda **kw: _FailClient())

    result = app_mod._call_servicenow_mcp("short", "desc", "user@bwater.com")

    assert result["_path"] == "stub"
    assert result["number"].startswith("INC")
    assert result["caller_id"] == "user@bwater.com"


def test_stub_path_when_url_unset(monkeypatch):
    """When SERVICENOW_MCP_URL is empty, use in-process stub without attempting wire."""
    import agent.app as app_mod

    monkeypatch.setattr(app_mod, "SERVICENOW_MCP_URL", "")

    result = app_mod._call_servicenow_mcp("short", "desc", "local@test.com")

    assert result["_path"] == "stub"
    assert result["caller_id"] == "local@test.com"
    assert result["number"].startswith("INC")
