from dataclasses import dataclass
from datetime import datetime

@dataclass
class WorkflowStatus:
    run_id: str
    thread_id: str
    user_id: str
    stage: str          # queued | running | complete | failed
    detail: str
    result: dict | None
    updated_at: datetime
    job_run_id: int | None = None

@dataclass
class Findings:
    host: str
    summary: str
    severity: str
    indicators: list[str]
