"""HITL incident approval: on completion the agent PROPOSES a ServiceNow incident
and waits; the write happens only on explicit approve_incident() — deny-by-default.
The ServiceNow MCP is mocked; no external call is made in these tests."""
from datetime import datetime, timezone

import agent.app as app_mod
from agent.status_store import InMemoryStatusStore
from common.models import WorkflowStatus


def _complete_run(store, run_id="r1", user="ryan@bw.com"):
    """A finished run whose result is the raw findings the job wrote."""
    store.upsert(WorkflowStatus(
        run_id=run_id, thread_id="t", user_id=user, stage="complete", detail="done",
        result={"host": "web-prod-04", "summary": "45 failed logins", "severity": "high"},
        updated_at=datetime.now(timezone.utc), job_run_id=None))
    return run_id


def _mock_mcp(monkeypatch, calls):
    monkeypatch.setattr(
        app_mod, "_call_servicenow_mcp",
        lambda short_description, description, caller_id:
            calls.__setitem__("n", calls["n"] + 1)
            or {"number": "INC0042042", "caller_id": caller_id, "state": "New"})


def test_complete_proposes_and_does_not_file(monkeypatch):
    calls = {"n": 0}
    _mock_mcp(monkeypatch, calls)
    store = InMemoryStatusStore()
    rid = _complete_run(store)
    resp = app_mod.check_status(rid, "web-prod-04", "ryan@bw.com", store)
    assert resp["awaiting_approval"] is True
    assert resp["incident"] is None
    assert calls["n"] == 0                                   # NOTHING filed on completion
    assert store.get(rid).result.get("proposed_incident")    # proposal persisted
    assert resp["proposal"]["short_description"] == "Suspicious logins on web-prod-04"


def test_repolling_reproposes_idempotently(monkeypatch):
    calls = {"n": 0}
    _mock_mcp(monkeypatch, calls)
    store = InMemoryStatusStore()
    rid = _complete_run(store)
    a = app_mod.check_status(rid, "web-prod-04", "ryan@bw.com", store)
    b = app_mod.check_status(rid, "web-prod-04", "ryan@bw.com", store)   # the 5s poller again
    assert a["proposal"] == b["proposal"]
    assert b["awaiting_approval"] is True and calls["n"] == 0


def test_approve_files_incident_once(monkeypatch):
    calls = {"n": 0}
    _mock_mcp(monkeypatch, calls)
    store = InMemoryStatusStore()
    rid = _complete_run(store)
    app_mod.check_status(rid, "web-prod-04", "ryan@bw.com", store)       # propose
    r1 = app_mod.approve_incident(rid, "ryan@bw.com", store)
    assert r1["incident"]["number"] == "INC0042042"
    assert r1["incident"]["caller_id"] == "ryan@bw.com"                  # filed as the user
    assert calls["n"] == 1
    # Idempotent: a second approve (double-click / racing poll) does not re-file.
    r2 = app_mod.approve_incident(rid, "ryan@bw.com", store)
    assert calls["n"] == 1
    assert r2["incident"]["number"] == "INC0042042"


def test_approve_without_proposal_files_nothing(monkeypatch):
    calls = {"n": 0}
    _mock_mcp(monkeypatch, calls)
    store = InMemoryStatusStore()
    rid = _complete_run(store)                                           # never proposed
    r = app_mod.approve_incident(rid, "ryan@bw.com", store)
    assert r["incident"] is None and calls["n"] == 0


def test_approve_after_decline_never_files(monkeypatch):
    """Deny-by-default: once declined, a stale/racing approve must NOT file or
    resurrect the incident."""
    calls = {"n": 0}
    _mock_mcp(monkeypatch, calls)
    store = InMemoryStatusStore()
    rid = _complete_run(store)
    app_mod.check_status(rid, "web-prod-04", "ryan@bw.com", store)   # propose
    app_mod.decline_incident(rid, "ryan@bw.com", store)             # decline
    r = app_mod.approve_incident(rid, "ryan@bw.com", store)          # racing approve
    assert r["incident"] is None and calls["n"] == 0


def test_failed_filing_releases_claim_for_retry(monkeypatch):
    """If the ServiceNow write raises, the claim is released so the human can retry
    (no permanent 'filing already in progress' wedge)."""
    store = InMemoryStatusStore()
    rid = _complete_run(store)
    app_mod.check_status(rid, "web-prod-04", "ryan@bw.com", store)   # propose
    calls = {"n": 0}

    def flaky(short_description, description, caller_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("mcp/lakebase down")
        return {"number": "INC0042007", "caller_id": caller_id, "state": "New"}

    monkeypatch.setattr(app_mod, "_call_servicenow_mcp", flaky)
    r1 = app_mod.approve_incident(rid, "ryan@bw.com", store)         # fails → releases claim
    assert r1["incident"] is None
    r2 = app_mod.approve_incident(rid, "ryan@bw.com", store)         # retry succeeds
    assert r2["incident"]["number"] == "INC0042007" and calls["n"] == 2


def test_decline_skips_filing_and_stops_reproposing(monkeypatch):
    calls = {"n": 0}
    _mock_mcp(monkeypatch, calls)
    store = InMemoryStatusStore()
    rid = _complete_run(store)
    app_mod.check_status(rid, "web-prod-04", "ryan@bw.com", store)       # propose
    d = app_mod.decline_incident(rid, "ryan@bw.com", store)
    assert d["incident"] is None and calls["n"] == 0
    # A later status read shows declined and does NOT re-propose.
    resp = app_mod.check_status(rid, "web-prod-04", "ryan@bw.com", store)
    assert not resp.get("awaiting_approval")
    assert any("declined" in line for line in resp["status_lines"])
