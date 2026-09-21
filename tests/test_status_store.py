from datetime import datetime, timezone
from common.models import WorkflowStatus
from agent.status_store import InMemoryStatusStore

def _mk(stage, detail, run_id="r1"):
    return WorkflowStatus(run_id=run_id, thread_id="t1", user_id="u",
                          stage=stage, detail=detail, result=None,
                          updated_at=datetime.now(timezone.utc))

def test_upsert_then_get():
    s = InMemoryStatusStore()
    s.upsert(_mk("queued", "waiting"))
    assert s.get("r1").stage == "queued"

def test_upsert_overwrites_by_run_id():
    s = InMemoryStatusStore()
    s.upsert(_mk("queued", "waiting"))
    s.upsert(_mk("running", "1/3"))
    assert s.get("r1").stage == "running" and s.get("r1").detail == "1/3"

def test_get_missing_returns_none():
    assert InMemoryStatusStore().get("nope") is None
