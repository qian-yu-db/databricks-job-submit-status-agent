import pytest
from agent.status_store import InMemoryStatusStore
from jobs.failure_demo_job import run as fail_run
from jobs.investigation_job import run as invest_run


def test_failure_demo_job_raises_analysis_exception():
    """The dedicated failure-demo job raises the simulated Spark/UC error (which the
    app detects via the Jobs API and diagnoses). It ignores launch args."""
    with pytest.raises(RuntimeError, match="auth_events_stg"):
        fail_run(run_id="r1", thread_id="t1", user_id="u", host="fail-demo-01", store=None)


def test_investigation_job_no_longer_special_cases_fail_demo_host():
    """fail-demo-01 is no longer magic to the investigation job — it just
    investigates and completes (failure now lives in its own job)."""
    store = InMemoryStatusStore()
    invest_run(run_id="r2", thread_id="t2", user_id="u", host="fail-demo-01",
               delay=0, store=store)
    assert store.get("r2").stage == "complete"
