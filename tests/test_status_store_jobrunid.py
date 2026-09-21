from datetime import datetime, timezone
from agent.status_store import InMemoryStatusStore
from common.models import WorkflowStatus

def _st(**kw):
    base = dict(run_id="r1", thread_id="t1", user_id="u", stage="queued",
                detail="d", result=None, updated_at=datetime.now(timezone.utc))
    base.update(kw)
    return WorkflowStatus(**base)

def test_set_and_get_job_run_id():
    s = InMemoryStatusStore()
    s.upsert(_st())
    s.set_job_run_id("r1", 999888)
    assert s.get("r1").job_run_id == 999888

def test_upsert_does_not_null_existing_job_run_id():
    s = InMemoryStatusStore()
    s.upsert(_st())
    s.set_job_run_id("r1", 999888)
    s.upsert(_st(stage="running", detail="1/3"))   # job upsert, no job_run_id
    assert s.get("r1").job_run_id == 999888
