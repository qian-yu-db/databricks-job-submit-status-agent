from datetime import datetime, timezone
from common.models import WorkflowStatus, Findings

def test_workflow_status_fields():
    s = WorkflowStatus(run_id="123", thread_id="t1", user_id="u@x.com",
                       stage="running", detail="1/3", result=None,
                       updated_at=datetime.now(timezone.utc))
    assert s.run_id == "123" and s.stage == "running" and s.result is None

def test_findings_serializable():
    f = Findings(host="web-prod-04", summary="3 suspicious logins",
                 severity="medium", indicators=["1.2.3.4"])
    assert f.host == "web-prod-04" and "1.2.3.4" in f.indicators
