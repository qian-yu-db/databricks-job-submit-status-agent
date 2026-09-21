from agent.stages import investigation_stages, build_findings
from jobs.investigation_job import run
from agent.status_store import InMemoryStatusStore

def test_stages_are_ordered_and_terminal():
    st = investigation_stages()
    assert st[0][0] == "running"
    assert st[-1][0] == "complete"
    assert len(st) >= 3

def test_build_findings_uses_host():
    f = build_findings("web-prod-04")
    assert f.host == "web-prod-04"
    assert f.severity in {"low", "medium", "high"}
    assert f.indicators

def test_investigation_run_writes_terminal_complete():
    store = InMemoryStatusStore()
    run(run_id="r9", thread_id="t", user_id="u", host="web-prod-04", delay=0, store=store)
    final = store.get("r9")
    assert final.stage == "complete" and final.result["host"] == "web-prod-04"
