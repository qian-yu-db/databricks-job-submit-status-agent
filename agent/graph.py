"""Small pure helpers used by the orchestrator (agent/app.py): status-line
formatting and findings selection. (This module once also held a hand-built
LangGraph StateGraph; that was superseded by the prebuilt ReAct agent in
agent/llm_agent.py and has been removed.)"""
from common.models import WorkflowStatus

def format_status_message(status: WorkflowStatus) -> str:
    prefix = {"queued": "[queued]", "running": "[running]", "complete": "[done]", "failed": "[failed]"}
    return f"{prefix.get(status.stage, '')} {status.detail}"

def select_findings(state: dict):
    """Findings that drive the incident. Prefer OBO findings (the auth-log query
    executed under the originating user's identity via their forwarded token —
    Unity Catalog enforced their grants) over the job's own result."""
    return state.get("obo_findings") or state.get("result")
