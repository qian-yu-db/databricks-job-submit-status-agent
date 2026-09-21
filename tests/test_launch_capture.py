import agent.app as app_mod
from agent.status_store import InMemoryStatusStore


def test_launch_job_captures_databricks_run_id(monkeypatch):
    class _Run: run_id = 777123
    class _Jobs:
        def run_now(self, **k): return _Run()
    class _WC: jobs = _Jobs()
    monkeypatch.setattr(app_mod, "WorkspaceClient", lambda *a, **k: _WC())
    monkeypatch.setattr(app_mod, "WAREHOUSE_ID", "wh")
    monkeypatch.setattr(app_mod, "AUTH_TABLE", "cat.sch.auth")
    store = InMemoryStatusStore()
    app_mod.launch_job("web-prod-04", "r1", "t1", "u@x.com", store, job_id="519")
    assert store.get("r1").job_run_id == 777123


def test_launch_job_uses_passed_job_id(monkeypatch):
    launched = {}
    class _Run: run_id = 1
    class _Jobs:
        def run_now(self, **k):
            launched["job_id"] = k["job_id"]; return _Run()
    class _WC: jobs = _Jobs()
    monkeypatch.setattr(app_mod, "WorkspaceClient", lambda *a, **k: _WC())
    monkeypatch.setattr(app_mod, "WAREHOUSE_ID", "wh")
    monkeypatch.setattr(app_mod, "AUTH_TABLE", "cat.sch.auth")
    store = InMemoryStatusStore()
    app_mod.launch_job("web-prod-04", "r", "t", "u@x.com", store, job_id="111")
    assert launched["job_id"] == 111
