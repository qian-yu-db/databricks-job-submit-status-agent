"""Async conversational flow: intent routing, non-blocking submit, and
status-on-demand that files the incident exactly once on completion."""

from datetime import datetime, timezone

import agent.app as app_mod
import agent.job_registry as jr
from agent.status_store import InMemoryStatusStore
from common.models import WorkflowStatus, Findings


def test_submit_is_nonblocking_and_runs_obo(monkeypatch):
    launched = {}
    monkeypatch.setattr(app_mod, "get_registry", lambda: {
        "investigation": jr.JobSpec("investigation", "I", "d", 999,
                                    {"host": {"type": "string", "required": True}}, {})
    })
    monkeypatch.setattr(app_mod, "launch_job",
                        lambda host, run_id, tid, uid, store, *, job_id: launched.update(host=host, run_id=run_id))
    monkeypatch.setattr(app_mod, "obo_investigate",
                        lambda host, tok: Findings(host=host, summary="45 failed then 5 success",
                                                   severity="high", indicators=[]))
    monkeypatch.setattr(app_mod, "WAREHOUSE_ID", "wh")
    monkeypatch.setattr(app_mod, "AUTH_TABLE", "cat.sch.auth")

    resp = app_mod.submit_investigation("web-prod-04", "ryan@bw.com", "tok", InMemoryStatusStore())

    assert resp["stage"] == "queued"                 # returns at submit, not 'complete'
    assert resp.get("incident") is None              # no incident filed at submit
    assert resp["run_id"] == launched["run_id"]      # the launched run is the one returned
    joined = "\n".join(resp["status_lines"])
    assert "[obo]" in joined and "as ryan@bw.com" in joined     # OBO proof present
    assert "45 failed then 5 success" in joined                 # finding shown at submit
    assert "status" in joined                                   # invites a status check


def test_submit_without_token_skips_obo(monkeypatch):
    monkeypatch.setattr(app_mod, "get_registry", lambda: {
        "investigation": jr.JobSpec("investigation", "I", "d", 999,
                                    {"host": {"type": "string", "required": True}}, {})
    })
    monkeypatch.setattr(app_mod, "launch_job", lambda *a, **k: None)
    monkeypatch.setattr(app_mod, "WAREHOUSE_ID", "wh")
    monkeypatch.setattr(app_mod, "AUTH_TABLE", "cat.sch.auth")
    resp = app_mod.submit_investigation("web-prod-04", "u", None, InMemoryStatusStore())
    assert "[obo]" not in "\n".join(resp["status_lines"])
    assert resp["stage"] == "queued"


def _row(stage, result=None):
    return WorkflowStatus(run_id="r1", thread_id="t1", user_id="ryan@bw.com",
                          stage=stage, detail="d", result=result,
                          updated_at=datetime.now(timezone.utc))


def test_status_running_does_not_file(monkeypatch):
    store = InMemoryStatusStore()
    store.upsert(_row("running"))
    calls = {"n": 0}
    monkeypatch.setattr(app_mod, "_call_servicenow_mcp",
                        lambda **k: calls.__setitem__("n", calls["n"] + 1) or {"number": "X"})
    resp = app_mod.check_status("r1", "web-prod-04", "ryan@bw.com", store)
    assert resp["stage"] == "running"
    assert resp["incident"] is None
    assert calls["n"] == 0


def test_complete_proposes_then_approve_files_once(monkeypatch):
    # HITL: completion proposes (no write); explicit approval files exactly once.
    store = InMemoryStatusStore()
    store.upsert(_row("complete", result={"host": "web-prod-04", "summary": "s",
                                          "severity": "high", "indicators": []}))
    calls = {"n": 0}

    def fake_file(**k):
        calls["n"] += 1
        assert k["caller_id"] == "ryan@bw.com"     # incident attributed to the user
        return {"number": "INC0042099", "caller_id": k["caller_id"], "state": "New"}

    monkeypatch.setattr(app_mod, "_call_servicenow_mcp", fake_file)

    # Completion proposes — nothing written to ServiceNow yet.
    r1 = app_mod.check_status("r1", "web-prod-04", "ryan@bw.com", store)
    assert r1["awaiting_approval"] is True and r1["incident"] is None
    assert calls["n"] == 0

    # Explicit approval files exactly once.
    a1 = app_mod.approve_incident("r1", "ryan@bw.com", store)
    assert a1["incident"]["number"] == "INC0042099"
    assert calls["n"] == 1

    # A repeat approve (double-click / racing poll) must NOT double-file.
    a2 = app_mod.approve_incident("r1", "ryan@bw.com", store)
    assert calls["n"] == 1
    assert a2["incident"]["number"] == "INC0042099"


def test_incident_uses_runs_actual_host(monkeypatch):
    # The incident must use the host the run investigated, not the request's host.
    store = InMemoryStatusStore()
    store.upsert(_row("complete", result={"host": "db-replica-07", "summary": "s",
                                          "severity": "high", "indicators": []}))
    captured = {}
    monkeypatch.setattr(app_mod, "_call_servicenow_mcp",
                        lambda **k: captured.update(k) or {"number": "INC1", "caller_id": k["caller_id"]})
    # request carries the wrong/default host; run actually investigated db-replica-07
    app_mod.check_status("r1", "web-prod-04", "ryan@bw.com", store)   # propose (uses run host)
    app_mod.approve_incident("r1", "ryan@bw.com", store)             # files from the proposal
    assert "db-replica-07" in captured["short_description"]


def test_status_unknown_run():
    resp = app_mod.check_status("nope", "web-prod-04", "u", InMemoryStatusStore())
    assert resp["stage"] == "unknown"


# ---------------------------------------------------------------------------
# submit_investigation → registry resolution
# ---------------------------------------------------------------------------

def _reg():
    return {
        "investigation": jr.JobSpec("investigation", "I", "d", 363941843397741,
                                    {"host": {"type": "string", "required": True}}, {}),
        "failure_demo": jr.JobSpec("failure_demo", "F", "d", 363941843397741, {},
                                   {"host": "fail-demo-01"}),
    }


def test_submit_resolves_investigation_and_launches(monkeypatch):
    monkeypatch.setattr(app_mod, "get_registry", lambda: _reg())
    seen = {}
    monkeypatch.setattr(app_mod, "launch_job",
                        lambda host, rid, tid, uid, store, *, job_id: seen.update(host=host, job_id=job_id))
    monkeypatch.setattr(app_mod, "WAREHOUSE_ID", ""); monkeypatch.setattr(app_mod, "AUTH_TABLE", "")
    resp = app_mod.submit_investigation("web-prod-04", "u@x.com", None, InMemoryStatusStore())
    assert resp["stage"] == "queued" and seen["job_id"] == 363941843397741 and seen["host"] == "web-prod-04"


def test_submit_failure_demo_forces_host(monkeypatch):
    monkeypatch.setattr(app_mod, "get_registry", lambda: _reg())
    seen = {}
    monkeypatch.setattr(app_mod, "launch_job",
                        lambda host, rid, tid, uid, store, *, job_id: seen.update(host=host))
    monkeypatch.setattr(app_mod, "WAREHOUSE_ID", ""); monkeypatch.setattr(app_mod, "AUTH_TABLE", "")
    resp = app_mod.submit_investigation("web-prod-04", "u@x.com", None,
                                        InMemoryStatusStore(), job_key="failure_demo")
    assert resp["stage"] == "queued" and seen["host"] == "fail-demo-01"


def test_submit_unknown_key_errors_without_launch(monkeypatch):
    monkeypatch.setattr(app_mod, "get_registry", lambda: _reg())
    called = {"n": 0}
    monkeypatch.setattr(app_mod, "launch_job", lambda *a, **k: called.__setitem__("n", 1))
    resp = app_mod.submit_investigation("h", "u@x.com", None, InMemoryStatusStore(), job_key="nope")
    assert resp["stage"] == "error" and called["n"] == 0


def test_submit_registry_unavailable_errors(monkeypatch):
    def _boom():
        raise jr.RegistryUnavailable("db down")
    monkeypatch.setattr(app_mod, "get_registry", _boom)
    called = {"n": 0}
    monkeypatch.setattr(app_mod, "launch_job", lambda *a, **k: called.__setitem__("n", 1))
    resp = app_mod.submit_investigation("h", "u@x.com", None, InMemoryStatusStore())
    assert resp["stage"] == "error" and called["n"] == 0
