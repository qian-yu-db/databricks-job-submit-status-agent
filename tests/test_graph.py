from datetime import datetime, timezone
from common.models import WorkflowStatus
from agent.graph import format_status_message, select_findings
from agent.obo import user_identity

def _st(stage, detail): return WorkflowStatus("r","t","ryan@bwater.com",stage,detail,None,
                                              datetime.now(timezone.utc))

def test_format_status_message_running():
    assert "correlating" in format_status_message(_st("running","correlating auth logs (1/3)"))

def test_user_identity_from_forwarded_header():
    assert user_identity({"x-forwarded-email": "ryan@bwater.com"}) == "ryan@bwater.com"

def test_select_findings_prefers_obo():
    # OBO findings (queried under the user's identity) win over the job result.
    state = {"obo_findings": {"summary": "as-user"}, "result": {"summary": "as-sp"}}
    assert select_findings(state) == {"summary": "as-user"}

def test_select_findings_falls_back_to_job_result():
    assert select_findings({"result": {"summary": "as-sp"}}) == {"summary": "as-sp"}
    assert select_findings({}) is None
