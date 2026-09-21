from datetime import datetime, timezone
import agent.app as app_mod
from agent.status_store import InMemoryStatusStore
from common.models import WorkflowStatus

def _row(stage, job_run_id=555, result=None):
    return WorkflowStatus(run_id="r1", thread_id="t1", user_id="u@x.com", stage=stage,
                          detail="d", result=result, updated_at=datetime.now(timezone.utc),
                          job_run_id=job_run_id)

def test_running_in_lakebase_but_databricks_failed_triggers_diagnosis(monkeypatch):
    store = InMemoryStatusStore(); store.upsert(_row("running"))
    monkeypatch.setattr(app_mod, "_databricks_result_state", lambda jr: "FAILED")
    calls = {"n": 0}
    def fake_diag(job_run_id):
        calls["n"] += 1
        return "Root cause: missing table. Fix: create it."
    monkeypatch.setattr(app_mod, "_diagnose_failed_run", fake_diag)
    r1 = app_mod.check_status("r1", "web-prod-04", "u@x.com", store)
    assert r1["stage"] == "failed"
    assert any("Root cause" in l for l in r1["status_lines"])
    assert calls["n"] == 1
    # cached: a repeat check reuses, does not re-diagnose
    r2 = app_mod.check_status("r1", "web-prod-04", "u@x.com", store)
    assert calls["n"] == 1
    assert r2["stage"] == "failed"

def test_transient_diagnosis_failure_is_not_cached_and_retries(monkeypatch):
    """A transient diagnosis failure must NOT be cached (which would poison the run
    forever); a later poll re-attempts and succeeds."""
    store = InMemoryStatusStore(); store.upsert(_row("running"))
    monkeypatch.setattr(app_mod, "_databricks_result_state", lambda jr: "FAILED")
    calls = {"n": 0}
    def flaky(job_run_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("model blip")
        return "Root cause: missing table."
    monkeypatch.setattr(app_mod, "_diagnose_failed_run", flaky)

    r1 = app_mod.check_status("r1", "web-prod-04", "u@x.com", store)
    assert r1["stage"] == "failed"
    assert any("temporarily unavailable" in l for l in r1["status_lines"])
    # NOT cached, and the claim was released so a retry is possible
    res = store.get("r1").result or {}
    assert not res.get("diagnosis") and not res.get("diagnosis_claimed")

    r2 = app_mod.check_status("r1", "web-prod-04", "u@x.com", store)
    assert calls["n"] == 2
    assert any("Root cause" in l for l in r2["status_lines"])
    assert (store.get("r1").result or {}).get("diagnosis")


def test_concurrent_diagnosis_claim_lost_reports_generating(monkeypatch):
    """If another caller already claimed the diagnosis, this call reports 'generating'
    and does NOT invoke the LLM (no double-fire)."""
    store = InMemoryStatusStore(); store.upsert(_row("running"))
    monkeypatch.setattr(app_mod, "_databricks_result_state", lambda jr: "FAILED")
    called = {"n": 0}
    monkeypatch.setattr(app_mod, "_diagnose_failed_run",
                        lambda jr: called.__setitem__("n", called["n"] + 1) or "x")
    assert store.claim_diagnosis_slot("r1") is True     # someone else claimed first
    r = app_mod.check_status("r1", "web-prod-04", "u@x.com", store)
    assert r["stage"] == "failed"
    assert any("generating" in l for l in r["status_lines"])
    assert called["n"] == 0                              # LLM not invoked by the loser


def test_running_and_databricks_running_stays_running(monkeypatch):
    store = InMemoryStatusStore(); store.upsert(_row("running"))
    monkeypatch.setattr(app_mod, "_databricks_result_state", lambda jr: None)  # not terminal
    r = app_mod.check_status("r1", "web-prod-04", "u@x.com", store)
    assert r["stage"] == "running"


def test_internal_error_life_cycle_detected_as_failure(monkeypatch):
    """life_cycle_state=INTERNAL_ERROR (hard crash) with result_state=None must
    surface as stage='failed' in check_status (Finding 4)."""
    from unittest.mock import MagicMock

    # Build a mock run where state.result_state is None but
    # state.life_cycle_state.value == "INTERNAL_ERROR".
    mock_lcs = MagicMock()
    mock_lcs.value = "INTERNAL_ERROR"
    mock_state = MagicMock()
    mock_state.result_state = None
    mock_state.life_cycle_state = mock_lcs
    mock_run = MagicMock()
    mock_run.state = mock_state

    mock_wc = MagicMock()
    mock_wc.jobs.get_run.return_value = mock_run
    monkeypatch.setattr(app_mod, "WorkspaceClient", lambda: mock_wc)

    # _databricks_result_state should now return "INTERNAL_ERROR"
    assert app_mod._databricks_result_state(555) == "INTERNAL_ERROR"

    # And check_status should treat it as failed.
    store = InMemoryStatusStore(); store.upsert(_row("running", job_run_id=555))
    monkeypatch.setattr(app_mod, "_diagnose_failed_run",
                        lambda jr: "Hard crash — no result state.")
    r = app_mod.check_status("r1", "web-prod-04", "u@x.com", store)
    assert r["stage"] == "failed"
    assert any("Hard crash" in l for l in r["status_lines"])
