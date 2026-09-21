"""
Integration tests for the /chat endpoint (LLM agent only).

The app runs the LangGraph agent for every typed turn; there is no deterministic
keyword router. When route_and_run raises (e.g. the model endpoint is down) the
endpoint returns a graceful error — not a keyword-routed fallback.
"""
import agent.app as app_mod
from unittest.mock import MagicMock


def test_chat_routes_through_agent(monkeypatch):
    """A typed turn goes through route_and_run."""
    monkeypatch.setattr(app_mod, "lakebase_conn", lambda **kw: MagicMock())
    monkeypatch.setattr(app_mod, "PostgresStatusStore", lambda conn: MagicMock())
    monkeypatch.setattr(app_mod, "lakebase_checkpointer", lambda: (MagicMock(), MagicMock()))
    monkeypatch.setattr(app_mod, "route_and_run",
                        lambda *a, **k: {"intent": "agent", "stage": "queued",
                                         "run_id": "a1", "host": "web-prod-04",
                                         "status_lines": []})
    from fastapi.testclient import TestClient
    client = TestClient(app_mod.app)
    r = client.post("/chat", json={"message": "investigate web-prod-04"})
    assert r.status_code == 200
    assert r.json()["intent"] == "agent"


def test_bare_chatter_reaches_agent(monkeypatch):
    """Fresh chatter with no run_id (e.g. 'run the failure demo') reaches the agent
    so it can select a workflow — there is no deterministic help short-circuit."""
    hit = {"n": 0}
    monkeypatch.setattr(app_mod, "route_and_run",
                        lambda *a, **k: hit.__setitem__("n", 1) or
                        {"intent": "agent", "stage": "queued", "run_id": "a1",
                         "host": "fail-demo-01", "status_lines": []})
    monkeypatch.setattr(app_mod, "lakebase_conn", lambda **kw: MagicMock())
    monkeypatch.setattr(app_mod, "PostgresStatusStore", lambda conn: MagicMock())
    monkeypatch.setattr(app_mod, "lakebase_checkpointer", lambda: (MagicMock(), MagicMock()))
    from fastapi.testclient import TestClient
    client = TestClient(app_mod.app)
    r = client.post("/chat", json={"message": "run the failure demo"})
    assert r.status_code == 200
    assert hit["n"] == 1
    assert r.json()["intent"] == "agent"


def test_agent_error_returns_graceful_error_no_router(monkeypatch):
    """When route_and_run raises (FM endpoint down), /chat returns a graceful error
    and does NOT fall back to any keyword router or launch a job."""
    monkeypatch.setattr(
        app_mod, "route_and_run",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fm down")),
    )
    monkeypatch.setattr(app_mod, "lakebase_conn", lambda **kw: MagicMock())
    monkeypatch.setattr(app_mod, "PostgresStatusStore", lambda conn: MagicMock())
    monkeypatch.setattr(app_mod, "lakebase_checkpointer", lambda: (MagicMock(), MagicMock()))
    submit_calls = {"n": 0}
    monkeypatch.setattr(app_mod, "submit_investigation",
                        lambda *a, **k: submit_calls.__setitem__("n", submit_calls["n"] + 1) or {})
    from fastapi.testclient import TestClient
    client = TestClient(app_mod.app)
    # With and without a run_id, an agent failure yields a graceful error, no launch.
    for body in ({"message": "investigate web-prod-04"},
                 {"message": "why did it fail?", "run_id": "r1", "host": "web-prod-04"}):
        r = client.post("/chat", json=body)
        assert r.status_code == 200
        data = r.json()
        assert data["intent"] == "error" and data["stage"] == "error"
        assert "unavailable" in " ".join(data["status_lines"]).lower()
    assert submit_calls["n"] == 0     # no deterministic submit/launch fallback


def test_followup_with_run_id_reaches_agent(monkeypatch):
    """A follow-up like 'why did it fail?' with an active run_id routes to the agent
    (so diagnose_run is reachable)."""
    called = {}

    def _fake_route(*a, **k):
        called["hit"] = True
        return {"intent": "agent", "stage": "failed", "run_id": "r1",
                "host": "fail-demo-01", "status_lines": ["[diagnosis] ..."]}

    monkeypatch.setattr(app_mod, "route_and_run", _fake_route)
    monkeypatch.setattr(app_mod, "lakebase_conn", lambda **kw: MagicMock())
    monkeypatch.setattr(app_mod, "PostgresStatusStore", lambda conn: MagicMock())
    monkeypatch.setattr(app_mod, "lakebase_checkpointer", lambda: (MagicMock(), MagicMock()))
    from fastapi.testclient import TestClient
    client = TestClient(app_mod.app)
    r = client.post("/chat", json={"message": "why did it fail?", "run_id": "r1",
                                    "host": "fail-demo-01"})
    assert r.status_code == 200
    assert called.get("hit") is True
    assert r.json()["intent"] == "agent"


def test_background_ping_bypasses_agent_and_calls_check_status(monkeypatch):
    """background:true must answer via check_status directly — never route_and_run."""
    called = {"agent": 0}
    monkeypatch.setattr(app_mod, "route_and_run",
                        lambda *a, **k: called.__setitem__("agent", 1) or {})
    monkeypatch.setattr(app_mod, "lakebase_conn", lambda **kw: MagicMock())
    monkeypatch.setattr(app_mod, "PostgresStatusStore", lambda conn: MagicMock())
    monkeypatch.setattr(app_mod, "check_status",
                        lambda run_id, host, uid, store: {"run_id": run_id, "host": host,
                                                          "stage": "complete", "status_lines": ["[done] ok"],
                                                          "incident": None})
    from fastapi.testclient import TestClient
    client = TestClient(app_mod.app)
    r = client.post("/chat", json={"message": "status", "run_id": "r1", "host": "web-prod-04",
                                   "background": True})
    assert r.status_code == 200
    assert r.json()["stage"] == "complete"
    assert called["agent"] == 0            # LLM never invoked for a background ping


def test_foreground_passes_composed_thread_id(monkeypatch):
    """A typed turn composes thread_id from user_id + session_id and passes it."""
    seen = {}
    def _fake_route(message, run_id, host, uid, tok, store, *, checkpointer=None, thread_id=None):
        seen["thread_id"] = thread_id
        return {"intent": "agent", "stage": "queued", "run_id": "a1", "host": host,
                "status_lines": []}
    monkeypatch.setattr(app_mod, "route_and_run", _fake_route)
    monkeypatch.setattr(app_mod, "lakebase_conn", lambda **kw: MagicMock())
    monkeypatch.setattr(app_mod, "PostgresStatusStore", lambda conn: MagicMock())
    monkeypatch.setattr(app_mod, "lakebase_checkpointer", lambda: (MagicMock(), MagicMock()))
    from fastapi.testclient import TestClient
    client = TestClient(app_mod.app)
    r = client.post("/chat", json={"message": "investigate web-prod-04", "session_id": "sess-1"})
    assert r.status_code == 200
    assert seen["thread_id"].endswith(":sess-1")


def test_checkpointer_failure_falls_back_to_stateless(monkeypatch):
    """If the checkpointer can't be built, chat still works (stateless invoke)."""
    seen = {}
    def _fake_route(*a, **k):
        seen["checkpointer"] = k.get("checkpointer")
        return {"intent": "agent", "stage": "help", "status_lines": ["ok"]}
    monkeypatch.setattr(app_mod, "route_and_run", _fake_route)
    monkeypatch.setattr(app_mod, "lakebase_conn", lambda **kw: MagicMock())
    monkeypatch.setattr(app_mod, "PostgresStatusStore", lambda conn: MagicMock())
    def _boom():
        raise RuntimeError("lakebase down")
    monkeypatch.setattr(app_mod, "lakebase_checkpointer", _boom)
    from fastapi.testclient import TestClient
    client = TestClient(app_mod.app)
    r = client.post("/chat", json={"message": "investigate web-prod-04", "session_id": "s"})
    assert r.status_code == 200
    assert seen["checkpointer"] is None     # invoked stateless, no crash
