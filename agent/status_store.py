import json
import logging
from datetime import datetime, timezone
from typing import Protocol
from common.models import WorkflowStatus

logger = logging.getLogger(__name__)

class StatusStore(Protocol):
    def upsert(self, status: WorkflowStatus) -> None: ...
    def get(self, run_id: str) -> WorkflowStatus | None: ...
    def set_job_run_id(self, run_id: str, job_run_id: int) -> None: ...
    def claim_incident_slot(self, run_id: str) -> bool: ...
    def claim_diagnosis_slot(self, run_id: str) -> bool: ...
    def release_diagnosis_slot(self, run_id: str) -> None: ...
    def release_incident_slot(self, run_id: str) -> None: ...
    def open_turn(self, turn_id: str, thread_id: str, user_id: str) -> None: ...
    def close_turn(self, turn_id: str) -> None: ...

class InMemoryStatusStore:
    def __init__(self) -> None:
        self._d: dict[str, WorkflowStatus] = {}
        self._turns: dict[str, dict] = {}
    def upsert(self, status: WorkflowStatus) -> None:
        prev = self._d.get(status.run_id)
        if status.job_run_id is None and prev is not None:
            status.job_run_id = prev.job_run_id   # never null an existing capture
        self._d[status.run_id] = status
    def get(self, run_id: str) -> WorkflowStatus | None:
        return self._d.get(run_id)
    def set_job_run_id(self, run_id: str, job_run_id: int) -> None:
        st = self._d.get(run_id)
        if st is not None:
            st.job_run_id = job_run_id
    def claim_incident_slot(self, run_id: str) -> bool:
        st = self._d.get(run_id)
        if st is None:
            return False
        r = st.result if isinstance(st.result, dict) else {}
        if r.get("incident") or r.get("incident_claimed"):
            return False
        st.result = {**r, "incident_claimed": True}
        return True
    def claim_diagnosis_slot(self, run_id: str) -> bool:
        st = self._d.get(run_id)
        if st is None:
            return False
        r = st.result if isinstance(st.result, dict) else {}
        if r.get("diagnosis") or r.get("diagnosis_claimed"):
            return False
        st.result = {**r, "diagnosis_claimed": True}
        return True
    def release_diagnosis_slot(self, run_id: str) -> None:
        st = self._d.get(run_id)
        if st is None:
            return
        r = st.result if isinstance(st.result, dict) else {}
        if "diagnosis_claimed" in r:
            st.result = {k: v for k, v in r.items() if k != "diagnosis_claimed"}
    def release_incident_slot(self, run_id: str) -> None:
        st = self._d.get(run_id)
        if st is None:
            return
        r = st.result if isinstance(st.result, dict) else {}
        if "incident_claimed" in r:
            st.result = {k: v for k, v in r.items() if k != "incident_claimed"}
    def open_turn(self, turn_id: str, thread_id: str, user_id: str) -> None:
        self._turns.setdefault(
            turn_id, {"thread_id": thread_id, "user_id": user_id, "state": "open",
                      "created_at": datetime.now(timezone.utc)})
    def close_turn(self, turn_id: str) -> None:
        t = self._turns.get(turn_id)
        if t is not None and t["state"] == "open":
            t["state"] = "closed"

class PostgresStatusStore:
    """Lakebase-backed store. `conn` is a psycopg connection."""
    def __init__(self, conn) -> None:
        self._conn = conn
    def upsert(self, status: WorkflowStatus) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """INSERT INTO workflow_status
                   (run_id, thread_id, user_id, stage, detail, result, updated_at, job_run_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (run_id) DO UPDATE SET
                     stage=EXCLUDED.stage, detail=EXCLUDED.detail,
                     result=EXCLUDED.result, updated_at=EXCLUDED.updated_at,
                     job_run_id=COALESCE(EXCLUDED.job_run_id, workflow_status.job_run_id)""",
                (status.run_id, status.thread_id, status.user_id, status.stage,
                 status.detail, json.dumps(status.result) if status.result else None,
                 status.updated_at, status.job_run_id))
        self._conn.commit()
    def get(self, run_id: str) -> WorkflowStatus | None:
        with self._conn.cursor() as cur:
            cur.execute("""SELECT run_id, thread_id, user_id, stage, detail, result, updated_at, job_run_id
                           FROM workflow_status WHERE run_id=%s""", (run_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return WorkflowStatus(run_id=row[0], thread_id=row[1], user_id=row[2],
                              stage=row[3], detail=row[4],
                              result=row[5],
                              updated_at=row[6], job_run_id=row[7])
    def set_job_run_id(self, run_id: str, job_run_id: int) -> None:
        with self._conn.cursor() as cur:
            cur.execute("UPDATE workflow_status SET job_run_id=%s WHERE run_id=%s",
                        (job_run_id, run_id))
        self._conn.commit()
    def claim_incident_slot(self, run_id: str) -> bool:
        """Atomically grant the right to file the incident for run_id exactly once.

        Concurrent callers race on a single row-locked conditional UPDATE; only
        the first — when neither an incident nor a prior claim is recorded — gets
        a row back and returns True. Prevents duplicate ServiceNow incidents when
        two status checks observe completion at the same time."""
        with self._conn.cursor() as cur:
            cur.execute(
                """UPDATE workflow_status
                   SET result = jsonb_set(COALESCE(result,'{}'::jsonb),
                                          '{incident_claimed}', 'true'::jsonb)
                   WHERE run_id=%s
                     AND NOT (COALESCE(result,'{}'::jsonb) ? 'incident')
                     AND NOT (COALESCE(result,'{}'::jsonb) ? 'incident_claimed')
                   RETURNING run_id""",
                (run_id,))
            won = cur.fetchone() is not None
        self._conn.commit()
        return won

    def claim_diagnosis_slot(self, run_id: str) -> bool:
        """Atomically grant the right to generate the diagnosis for run_id exactly
        once (foreground 'why did it fail?' can race the background poller). Released
        on failure so a transient error retries; a written diagnosis replaces result
        and supersedes the claim."""
        with self._conn.cursor() as cur:
            cur.execute(
                """UPDATE workflow_status
                   SET result = jsonb_set(COALESCE(result,'{}'::jsonb),
                                          '{diagnosis_claimed}', 'true'::jsonb)
                   WHERE run_id=%s
                     AND NOT (COALESCE(result,'{}'::jsonb) ? 'diagnosis')
                     AND NOT (COALESCE(result,'{}'::jsonb) ? 'diagnosis_claimed')
                   RETURNING run_id""",
                (run_id,))
            won = cur.fetchone() is not None
        self._conn.commit()
        return won

    def release_diagnosis_slot(self, run_id: str) -> None:
        """Clear the diagnosis claim so a failed attempt can be retried on a later poll."""
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE workflow_status "
                "SET result = (COALESCE(result,'{}'::jsonb) - 'diagnosis_claimed') "
                "WHERE run_id=%s",
                (run_id,))
        self._conn.commit()

    def release_incident_slot(self, run_id: str) -> None:
        """Clear the incident claim so a failed approve can be retried (mirrors
        release_diagnosis_slot)."""
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE workflow_status "
                "SET result = (COALESCE(result,'{}'::jsonb) - 'incident_claimed') "
                "WHERE run_id=%s",
                (run_id,))
        self._conn.commit()

    def open_turn(self, turn_id: str, thread_id: str, user_id: str) -> None:
        """Record the start of a user turn (observability only). The sweeper job
        marks any turn still 'open' past its TTL as 'abandoned'; a completed turn
        is set 'closed' by close_turn so it's never swept.

        Best-effort: on failure we roll back and log rather than raise, so a turn
        write can never break the chat turn or leave the shared request connection
        in an aborted-transaction state that would fail later status reads/writes."""
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO turns (turn_id, thread_id, user_id, state)
                       VALUES (%s,%s,%s,'open')
                       ON CONFLICT (turn_id) DO NOTHING""",
                    (turn_id, thread_id, user_id))
            self._conn.commit()
        except Exception as exc:
            self._conn.rollback()
            logger.warning("open_turn failed (%s); turn not recorded", exc)

    def close_turn(self, turn_id: str) -> None:
        """Mark a finished turn 'closed'. This runs only when a request has actually
        completed, so it closes the turn regardless of prior state — which also
        *corrects* a premature 'abandoned' that a racing sweeper may have set. It
        rolls back first to clear any aborted-transaction state an earlier failed
        statement may have left on the shared request connection (so the close still
        records), and is best-effort: rolled back and logged on failure, never raised."""
        try:
            self._conn.rollback()   # recover the shared conn if a prior op aborted it
            with self._conn.cursor() as cur:
                cur.execute("UPDATE turns SET state='closed' WHERE turn_id=%s", (turn_id,))
            self._conn.commit()
        except Exception as exc:
            self._conn.rollback()
            logger.warning("close_turn failed (%s); turn left open for the sweeper", exc)
