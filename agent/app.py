"""
Investigation Agent — Orchestrator Databricks App (Task 8B).

FastAPI app that fronts the LangGraph investigation orchestrator:
  GET  /       → minimal chat HTML
  POST /chat   → runs the full workflow for the originating user,
                 returns ordered status lines + stub ServiceNow incident.

Gotcha solutions
----------------
1. Lakebase 1h token TTL: `lakebase_conn()` is a factory — every call
   generates a fresh credential via `w.postgres.generate_database_credential()`.
   Two separate connections are created per request (one for the status
   store, one for the PostgresSaver checkpointer) to avoid transaction
   state conflicts.  The local smoke test job thread also creates its
   own connection.

2. Job parameter passing: `trigger_job()` passes parameters as
   `python_params` (sys.argv) when invoking the Databricks Job.
   `investigation_job.main()` now reads argparse args (not env vars).
   Both ends are consistent for a serverless python task.
"""

from __future__ import annotations

import json as _json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)
from databricks.sdk import WorkspaceClient
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from agent.graph import format_status_message, select_findings
from agent.job_registry import get_registry, resolve_job, RegistryError, RegistryUnavailable
from agent.obo import user_identity
from agent.status_store import PostgresStatusStore
from agent.tracing import enable_tracing
from agent.llm_agent import route_and_run, _flatten_content, _compose_thread_id  # cheap import; heavy LLM deps load lazily on use
from common.models import WorkflowStatus

# ---------------------------------------------------------------------------
# Configuration (all from env — no hardcoded secrets)
# ---------------------------------------------------------------------------
LAKEBASE_ENDPOINT = os.environ.get(
    "LAKEBASE_ENDPOINT",
    "projects/job-agent/branches/production/endpoints/primary",
)
LAKEBASE_HOST = os.environ.get(
    "PGHOST", "ep-cold-paper-d2qf2m85.database.us-east-1.cloud.databricks.com"
)
LAKEBASE_HOSTADDR = os.environ.get("PGHOST_ADDR", "")   # macOS DNS workaround
LAKEBASE_DB = os.environ.get("PGDATABASE", "databricks_postgres")
LAKEBASE_USER = os.environ.get("PGUSER", "you@example.com")
LAKEBASE_PORT = int(os.environ.get("PGPORT", "5432"))

SERVICENOW_MCP_URL = os.environ.get("SERVICENOW_MCP_URL", "")
WAREHOUSE_ID = os.environ.get("WAREHOUSE_ID", "<your-sql-warehouse-id>")
AUTH_TABLE = os.environ.get("AUTH_TABLE", "main.job_agent_demo.auth_events")

# MLflow tracing for the LLM agent (non-fatal).
enable_tracing()

# ---------------------------------------------------------------------------
# Lakebase connection factory (Gotcha 1: 1h token TTL)
# ---------------------------------------------------------------------------
def _lakebase_token() -> str:
    """Fetch a fresh short-lived OAuth token for Lakebase.

    Uses LAKEBASE_TOKEN env var for local override (e.g., a pre-generated
    token from the CLI) so smoke tests don't require an SDK auth call.
    In the deployed App the SDK auto-detects injected SP creds.
    """
    if tok := os.environ.get("LAKEBASE_TOKEN"):
        return tok
    w = WorkspaceClient()
    cred = w.postgres.generate_database_credential(LAKEBASE_ENDPOINT)
    return cred.token


def lakebase_conn(autocommit: bool = False, row_factory=None) -> psycopg.Connection:
    """Create a fresh psycopg3 connection to Lakebase (never reuses).

    Always fetches a new token so callers are never blocked by the 1h TTL.
    macOS: set PGHOST_ADDR=<dig +short HOST> to bypass DNS resolution;
    host is still passed for TLS SNI.

    autocommit=True is required for the PostgresSaver (checkpoint) connection
    because LangGraph's setup() runs CREATE INDEX CONCURRENTLY which cannot
    execute inside a transaction block (psycopg3 default).
    row_factory: optional psycopg row factory (e.g. dict_row for the checkpointer).
    """
    token = _lakebase_token()
    kwargs: dict[str, Any] = dict(
        host=LAKEBASE_HOST,
        dbname=LAKEBASE_DB,
        user=LAKEBASE_USER,
        password=token,
        port=LAKEBASE_PORT,
        sslmode="require",
        autocommit=autocommit,
    )
    if row_factory is not None:
        kwargs["row_factory"] = row_factory
    if LAKEBASE_HOSTADDR:
        kwargs["hostaddr"] = LAKEBASE_HOSTADDR
    return psycopg.connect(**kwargs)


_CHECKPOINTER_READY = False
_CHECKPOINTER_LOCK = threading.Lock()
# Dedicated schema so the app SP creates and OWNS its checkpoint tables. Avoids
# colliding with pre-existing public.checkpoint* tables owned by another role
# (which the SP can't write, causing setup()/writes to fail) and needs no manual
# grants — the SP has CAN_CONNECT_AND_CREATE, so it can create this schema.
_CHECKPOINTER_SCHEMA = "agent_memory"


def lakebase_checkpointer():
    """Return (PostgresSaver, connection) over a fresh autocommit Lakebase conn
    whose search_path points at the SP-owned `agent_memory` schema. Runs
    PostgresSaver.setup() once per process (idempotent DDL; needs autocommit for
    CREATE INDEX CONCURRENTLY). Caller closes the connection."""
    global _CHECKPOINTER_READY
    conn = lakebase_conn(autocommit=True, row_factory=dict_row)
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS {_CHECKPOINTER_SCHEMA}')
            cur.execute(f'SET search_path TO {_CHECKPOINTER_SCHEMA}, public')
        saver = PostgresSaver(conn)
        if not _CHECKPOINTER_READY:
            with _CHECKPOINTER_LOCK:
                if not _CHECKPOINTER_READY:
                    saver.setup()
                    _CHECKPOINTER_READY = True
        return saver, conn
    except Exception:
        conn.close()
        raise


# ---------------------------------------------------------------------------
# ServiceNow incident filing — proper MCP Streamable-HTTP handshake
# ---------------------------------------------------------------------------

def _mcp_auth_headers() -> dict[str, str]:
    """Return Authorization headers from the app's own Databricks SDK credentials.

    In the deployed app, WorkspaceClient() auto-detects the SP credentials
    injected by Databricks Apps.  Locally, picks up the active CLI profile.
    """
    try:
        return WorkspaceClient().config.authenticate()
    except Exception as exc:  # pragma: no cover
        logger.warning("MCP auth: SDK failed (%s); proceeding without auth header", exc)
        return {}


def _parse_mcp_sse_result(text: str) -> dict:
    """Extract the incident dict from an MCP Streamable-HTTP SSE response.

    FastMCP always returns text/event-stream.  Format:
        event: message
        data: {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"<json>"}]}}
    """
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            msg = _json.loads(line[5:].strip())
        except ValueError:
            continue
        if "error" in msg:
            raise ValueError(f"MCP error: {msg['error']}")
        result = msg.get("result", {})
        content = result.get("content", [])
        if not content:
            return result
        raw = content[0].get("text", "")
        try:
            return _json.loads(raw)
        except ValueError:
            return {"raw": raw}
    raise ValueError(f"No valid data line in SSE response: {text[:300]}")


def _call_servicenow_mcp_wire(
    short_description: str, description: str, caller_id: str
) -> dict:
    """Call the deployed MCP app via proper Streamable-HTTP handshake.

    Steps:
      1. POST /mcp  initialize  → capture mcp-session-id response header
      2. POST /mcp  notifications/initialized  (required ack, ignored by server)
      3. POST /mcp  tools/call create_incident  → parse SSE result
    """
    auth_headers = _mcp_auth_headers()
    hdrs: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **auth_headers,
    }
    url = f"{SERVICENOW_MCP_URL}/mcp"

    with httpx.Client(timeout=30) as client:
        # ── 1. initialize ────────────────────────────────────────────────
        init_resp = client.post(
            url,
            json={
                "jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "job-status-agent", "version": "1.0"},
                },
            },
            headers=hdrs,
        )
        init_resp.raise_for_status()
        session_id = init_resp.headers.get("mcp-session-id")
        if session_id:
            hdrs["Mcp-Session-Id"] = session_id
            logger.info("MCP wire: session established %.8s…", session_id)

        # ── 2. notifications/initialized (fire-and-forget ack) ───────────
        client.post(
            url,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=hdrs,
        )

        # ── 3. tools/call create_incident ────────────────────────────────
        tool_resp = client.post(
            url,
            json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {
                    "name": "create_incident",
                    "arguments": {
                        "short_description": short_description,
                        "description": description,
                        "caller_id": caller_id,
                    },
                },
            },
            headers=hdrs,
        )
        tool_resp.raise_for_status()
        logger.info("MCP wire: tools/call HTTP %s caller=%s", tool_resp.status_code, caller_id)
        return _parse_mcp_sse_result(tool_resp.text)


def _call_servicenow_mcp(
    short_description: str, description: str, caller_id: str
) -> dict:
    """Call ServiceNow MCP.

    - SERVICENOW_MCP_URL set: calls the deployed MCP app over Streamable-HTTP;
      automatically falls back to in-process StubBackend if the wire call fails.
    - SERVICENOW_MCP_URL unset: in-process StubBackend (local dev / CI).
    Identity passthrough: caller_id is the originating user's email.
    """
    if SERVICENOW_MCP_URL:
        try:
            result = _call_servicenow_mcp_wire(short_description, description, caller_id)
            result.setdefault("_path", "wire")
            logger.info(
                "ServiceNow incident via wire: %s caller=%s", result.get("number"), caller_id
            )
            return result
        except Exception as exc:
            logger.warning("Wire MCP call failed (%s); falling back to stub", exc)

    from servicenow_mcp.backend import StubBackend

    result = StubBackend().create_incident(
        short_description=short_description,
        description=description,
        caller_id=caller_id,
    )
    result["_path"] = "stub"
    logger.info("ServiceNow incident via stub: %s caller=%s", result.get("number"), caller_id)
    return result


# ---------------------------------------------------------------------------
# Identity passthrough (OBO): query the auth logs AS THE ORIGINATING USER
# ---------------------------------------------------------------------------
def obo_investigate(host: str, user_token: str):
    """Run the suspicious-logins query under the user's own identity.

    The forwarded `x-forwarded-access-token` (downscoped to the app's `sql`
    user_api_scope) authenticates the Statement Execution call, so Unity
    Catalog enforces the *originating user's* grants — `current_user()` is the
    user, not the app service principal.  This is real, GA, token-level OBO to
    a Databricks target; it is the platform-enforced proof of criterion #3.

    `auth_type="pat"` is required: the Apps runtime injects the SP's
    DATABRICKS_CLIENT_ID/SECRET, which would otherwise collide with the
    forwarded token ("more than one authorization method configured").
    """
    from databricks.sdk.core import Config
    from agent.investigation import investigate, sdk_sql_executor

    # Databricks Apps always injects DATABRICKS_HOST; avoid a bare Config()
    # (which would resolve default auth) when it's present.
    host_url = os.environ.get("DATABRICKS_HOST") or Config().host
    user_cfg = Config(host=host_url, token=user_token, auth_type="pat")
    user_wc = WorkspaceClient(config=user_cfg)
    return investigate(
        host, execute_sql=sdk_sql_executor(user_wc, WAREHOUSE_ID), table=AUTH_TABLE
    )


# ---------------------------------------------------------------------------
# Conversational async orchestration: submit (non-blocking) + status-on-demand
# ---------------------------------------------------------------------------

def launch_job(host: str, run_id: str, thread_id: str, user_id: str, store, *, job_id) -> None:
    """Record the run as queued and kick off the workflow at `job_id` (resolved from
    the registry). Deployed: a serverless Databricks Job. Local (job_id falsy): the
    job runs inline in a daemon thread with its own Lakebase connection."""
    store.upsert(WorkflowStatus(
        run_id=run_id, thread_id=thread_id, user_id=user_id,
        stage="queued", detail="job submitted", result=None,
        updated_at=datetime.now(timezone.utc),
    ))
    if job_id:
        resp = WorkspaceClient().jobs.run_now(
            job_id=int(job_id),
            python_params=[
                "--run-id", run_id, "--thread-id", thread_id, "--user-id", user_id,
                "--host", host, "--delay", "8",
                "--warehouse-id", WAREHOUSE_ID, "--auth-table", AUTH_TABLE,
            ],
        )
        if getattr(resp, "run_id", None) is not None:
            store.set_job_run_id(run_id, resp.run_id)
    else:
        def job_thread():
            job_conn = lakebase_conn()
            try:
                from jobs.investigation_job import run as job_run
                job_run(run_id=run_id, thread_id=thread_id, user_id=user_id,
                        host=host, delay=0.3, store=PostgresStatusStore(job_conn))
            finally:
                job_conn.close()
        threading.Thread(target=job_thread, daemon=True).start()


def submit_investigation(host: str, user_id: str, user_token: str | None, store,
                         *, job_key: str = "investigation") -> dict:
    """Resolve the workflow from the governed registry, query the auth logs AS the
    user (OBO), start the long-running job, and return its run_id without waiting."""
    try:
        registry = get_registry()
    except RegistryUnavailable as exc:
        logger.warning("job registry unavailable (%s)", exc)
        return {"run_id": "", "host": host, "stage": "error",
                "status_lines": ["[error] the workflow catalog is temporarily unavailable — try again"]}
    try:
        job_id, params = resolve_job(job_key, {"host": host}, registry)
    except RegistryError as exc:
        return {"run_id": "", "host": host, "stage": "error",
                "status_lines": [f"[error] {exc}"]}
    host = params.get("host", host)
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    lines = [f"[start] investigating {host} as {user_id}"]
    if user_token and WAREHOUSE_ID and AUTH_TABLE:
        try:
            f = obo_investigate(host, user_token)
            lines.append(
                f"[obo] queried {AUTH_TABLE} as {user_id} via your forwarded token "
                f"— Unity Catalog authorized access (no shared service account)"
            )
            lines.append(f"[finding] {f.summary} (severity: {f.severity})")
        except Exception as exc:
            logger.warning("OBO query failed (%s); job/mock findings will be used", exc)
    launch_job(host, run_id, thread_id, user_id, store, job_id=job_id)
    lines.append(
        f"[queued] run {run_id[:8]} started on serverless — "
        f"ask me “what’s the status?” anytime"
    )
    return {"run_id": run_id, "host": host, "thread_id": thread_id,
            "stage": "queued", "status_lines": lines}


def _databricks_result_state(job_run_id):
    """Return the Databricks run's result_state ('FAILED'/'SUCCESS'/...) or None.

    Hard crashes surface as life_cycle_state=INTERNAL_ERROR with result_state=None;
    we return "INTERNAL_ERROR" in that case so check_status treats them as failures.
    """
    if not job_run_id:
        return None
    try:
        run = WorkspaceClient().jobs.get_run(run_id=int(job_run_id))
        st = getattr(run, "state", None)
        rs = getattr(st, "result_state", None)
        if rs is not None:
            return getattr(rs, "value", rs)
        # Hard crash: life_cycle_state=INTERNAL_ERROR, result_state absent/None.
        lcs = getattr(st, "life_cycle_state", None)
        lcs_val = getattr(lcs, "value", lcs) if lcs is not None else None
        if lcs_val == "INTERNAL_ERROR":
            return "INTERNAL_ERROR"
        return None
    except Exception as exc:  # pragma: no cover
        logger.warning("get_run state failed (%s)", exc)
        return None


def _diagnose_failed_run(job_run_id):
    """Fetch the failed run's log (as the app SP) and return a grounded diagnosis."""
    from agent.diagnosis import fetch_run_error, diagnose
    from agent.llm_agent import get_model
    text = fetch_run_error(WorkspaceClient(), int(job_run_id))
    return diagnose(text, model=get_model())


def check_status(run_id: str, host: str | None, user_id: str, store) -> dict:
    """Status-on-demand: read the run from Lakebase and report its stage. On the
    first check that observes completion — guarded by an atomic claim so
    concurrent checks can't double-file — file the ServiceNow incident from the
    completed run's own findings, attributed to the user, for the host the run
    actually investigated (not the request's host)."""
    st = store.get(run_id) if run_id else None
    if st is None:
        return {"run_id": run_id, "host": host, "stage": "unknown",
                "status_lines": [f"[status] no run found — start one with "
                                 f"“investigate <host>”"]}
    lines = [f"[status] run {run_id[:8]}: {format_status_message(st)}"]
    incident = None
    # Detect a hard job failure the job couldn't self-report.
    if st.stage != "complete":
        existing = st.result if isinstance(st.result, dict) else None
        if existing and existing.get("diagnosis"):
            # _flatten_content guards diagnoses cached as raw block lists by an
            # older build (new diagnoses are already plain strings).
            return {"run_id": run_id, "host": host, "stage": "failed",
                    "status_lines": [f"[status] run {run_id[:8]}: [failed] {st.detail}",
                                     f"[diagnosis] {_flatten_content(existing['diagnosis'])}"],
                    "incident": None}
        state = _databricks_result_state(st.job_run_id)
        if st.stage == "failed" or state in ("FAILED", "INTERNAL_ERROR", "TIMEDOUT"):
            # Only one caller generates the diagnosis — a foreground "why did it
            # fail?" can race the 5s background poller. Losers report "generating".
            if not store.claim_diagnosis_slot(run_id):
                return {"run_id": run_id, "host": host, "stage": "failed",
                        "status_lines": [f"[status] run {run_id[:8]}: [failed] {st.detail}",
                                         "[diagnosis] generating — ask again in a moment"],
                        "incident": None}
            try:
                diagnosis = _diagnose_failed_run(st.job_run_id)
                # Cache ONLY a real diagnosis; the write replaces result (dropping
                # the claim flag) so later polls short-circuit to the cached text.
                store.upsert(WorkflowStatus(
                    run_id=run_id, thread_id=st.thread_id, user_id=st.user_id,
                    stage="failed", detail=st.detail,
                    result={"failed": True, "diagnosis": diagnosis},
                    updated_at=datetime.now(timezone.utc), job_run_id=st.job_run_id))
            except Exception as exc:
                # Transient (e.g. model blip): don't cache a fallback — release the
                # claim so a later poll re-attempts instead of poisoning the run.
                logger.warning("diagnosis failed (%s); releasing claim to retry", exc)
                store.release_diagnosis_slot(run_id)
                diagnosis = "Automatic diagnosis is temporarily unavailable — retrying; see the job run logs."
            return {"run_id": run_id, "host": host, "stage": "failed",
                    "status_lines": [f"[status] run {run_id[:8]}: [failed] {st.detail}",
                                     f"[diagnosis] {_flatten_content(diagnosis)}"],
                    "incident": None}
    if st.stage == "complete":
        existing = st.result if isinstance(st.result, dict) else {}
        if existing.get("incident"):
            incident = existing["incident"]
            lines.append(f"[done] incident {incident.get('number')} already filed as {user_id}")
        elif existing.get("incident_declined"):
            lines.append("[done] filing was declined — nothing was sent to ServiceNow")
        else:
            # HITL / deny-by-default: never write to ServiceNow without an explicit
            # human approval. On completion we PROPOSE the incident and wait; the
            # actual write happens only via approve_incident() (below). This is
            # idempotent — the 5s poller re-surfaces the same proposal until the
            # user approves or declines.
            # On the first poll st.result IS the raw findings (from the job); once we
            # store a proposal it's {findings, proposed_incident}, so read the nested
            # findings back out on re-polls.
            findings = existing.get("findings") or select_findings({"result": st.result})
            # Authoritative host: the one this run actually investigated
            # (from its findings), not whatever the request happened to carry.
            run_host = (findings or {}).get("host") or host or "an affected host"
            proposal = existing.get("proposed_incident") or {
                "short_description": f"Suspicious logins on {run_host}",
                "description": str(findings)}
            if not existing.get("proposed_incident"):
                store.upsert(WorkflowStatus(
                    run_id=run_id, thread_id=st.thread_id, user_id=st.user_id,
                    stage="complete", detail=st.detail,
                    result={"findings": findings, "proposed_incident": proposal},
                    updated_at=datetime.now(timezone.utc), job_run_id=st.job_run_id))
            lines.append("[approval] investigation complete — approval required before filing")
            lines.append(f"Proposed ServiceNow incident: **{proposal['short_description']}** "
                         f"— will be filed as {user_id}. Approve to file, or decline.")
            return {"run_id": run_id, "host": run_host, "stage": "complete",
                    "status_lines": lines, "incident": None,
                    "awaiting_approval": True, "proposal": proposal}
    return {"run_id": run_id, "host": host, "stage": st.stage,
            "status_lines": lines, "incident": incident}


def approve_incident(run_id: str, user_id: str, store) -> dict:
    """File the proposed incident for run_id, once, on explicit human approval.

    This is the only place a ServiceNow write happens. claim_incident_slot makes
    it fire exactly once even if the approve button is double-clicked or a poll
    races it."""
    st = store.get(run_id)
    if st is None:
        return {"run_id": run_id, "stage": "unknown", "incident": None,
                "status_lines": ["[error] no run found to approve"]}
    existing = st.result if isinstance(st.result, dict) else {}
    if existing.get("incident"):
        inc = existing["incident"]
        return {"run_id": run_id, "stage": "complete", "incident": inc,
                "status_lines": [f"[done] incident {inc.get('number')} already filed as {user_id}"]}
    if existing.get("incident_declined"):
        # Deny-by-default: a decline is final. A stale/racing approve must never
        # resurrect and file a declined incident.
        return {"run_id": run_id, "stage": "complete", "incident": None,
                "status_lines": ["[approval] this filing was declined — not sending to ServiceNow"]}
    proposal = existing.get("proposed_incident")
    if not proposal:
        return {"run_id": run_id, "stage": st.stage, "incident": None,
                "status_lines": ["[error] nothing to approve for this run"]}
    if not store.claim_incident_slot(run_id):
        return {"run_id": run_id, "stage": "complete", "incident": None,
                "status_lines": ["[approval] filing already in progress — ask again in a moment"]}
    findings = existing.get("findings")
    try:
        incident = _call_servicenow_mcp(
            short_description=proposal["short_description"],
            description=proposal.get("description", str(findings)), caller_id=user_id)
        store.upsert(WorkflowStatus(
            run_id=run_id, thread_id=st.thread_id, user_id=st.user_id,
            stage="complete", detail=st.detail,
            result={"findings": findings, "incident": incident},
            updated_at=datetime.now(timezone.utc)))
    except Exception as exc:
        # Release the claim so the human can retry, instead of wedging the run at
        # "filing already in progress" forever.
        store.release_incident_slot(run_id)
        logger.warning("incident filing failed (%s); released claim for retry", exc)
        return {"run_id": run_id, "stage": "complete", "incident": None,
                "status_lines": ["[error] filing failed — click Approve again to retry"]}
    return {"run_id": run_id, "stage": "complete", "incident": incident,
            "status_lines": [f"[approval] approved by {user_id}",
                             f"[done] filed ServiceNow incident {incident.get('number')} as {user_id}"]}


def decline_incident(run_id: str, user_id: str, store) -> dict:
    """Record that the user declined to file — no ServiceNow write, and the poller
    stops re-proposing."""
    st = store.get(run_id)
    if st is None:
        return {"run_id": run_id, "stage": "unknown", "incident": None,
                "status_lines": ["[error] no run found"]}
    existing = st.result if isinstance(st.result, dict) else {}
    if not existing.get("incident"):
        store.upsert(WorkflowStatus(
            run_id=run_id, thread_id=st.thread_id, user_id=st.user_id,
            stage="complete", detail=st.detail,
            result={**existing, "incident_declined": True},
            updated_at=datetime.now(timezone.utc)))
    return {"run_id": run_id, "stage": "complete", "incident": None,
            "status_lines": [f"[done] filing declined by {user_id} — nothing sent to ServiceNow"]}


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Investigation Agent")

_CHAT_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Investigation Agent</title>
<style>
  :root{
    --bg:#eaeef3;--surface:#fff;--ink:#17212f;--muted:#647082;--line:#dfe5ec;
    --brand:#3949ab;--brand-ink:#eef1ff;
    --amber:#b45309;--amber-bg:#fbf1e0;--emerald:#047857;--emerald-bg:#e6f5ee;
    --rose:#be123c;--rose-bg:#fdecef;--gutter:clamp(16px,calc(50% - 380px),50%);
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  body{margin:0;background:var(--bg);color:var(--ink);
       font-family:ui-sans-serif,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
       font-size:15px;line-height:1.5;display:flex;flex-direction:column;height:100vh}

  header{background:var(--surface);border-bottom:1px solid var(--line);
         padding:13px clamp(16px,4vw,28px);display:flex;align-items:center;gap:12px}
  .logo{width:34px;height:34px;flex:0 0 auto;display:grid;place-items:center;
        background:var(--brand);border-radius:9px;color:#fff}
  .logo svg{width:19px;height:19px;display:block}
  .htext h1{font-size:1rem;margin:0;font-weight:650;letter-spacing:-.01em}
  .htext p{margin:2px 0 0;font-size:.79rem;color:var(--muted)}
  .live{margin-left:auto;display:flex;align-items:center;gap:7px;font-size:.72rem;color:var(--muted)}
  .live .dot{width:8px;height:8px;border-radius:50%;background:var(--emerald);
             animation:pulse 2.4s infinite}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(4,120,87,.45)}
                   70%{box-shadow:0 0 0 7px rgba(4,120,87,0)}
                   100%{box-shadow:0 0 0 0 rgba(4,120,87,0)}}

  #thread{flex:1;overflow-y:auto;padding:22px var(--gutter);
          display:flex;flex-direction:column;gap:14px}
  .msg{max-width:80%;padding:11px 15px;border-radius:16px;font-size:.92rem;
       line-height:1.5;word-wrap:break-word;animation:rise .22s ease both}
  @keyframes rise{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
  .user{align-self:flex-end;background:var(--brand);color:#fff;
        border-bottom-right-radius:5px;white-space:pre-wrap}
  .agent{align-self:flex-start;background:var(--surface);color:var(--ink);
         border:1px solid var(--line);border-bottom-left-radius:5px;
         box-shadow:0 1px 2px rgba(23,33,47,.04)}

  /* investigation log — colored event tags carry the run's state */
  .logline{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.79rem;
           line-height:1.75;color:#38495d;white-space:pre-wrap}
  .proseline{white-space:pre-wrap}
  .proseline+.proseline,.proseline+.logline,.logline+.proseline{margin-top:.4em}
  .tag{display:inline-block;font-weight:600;font-size:.7rem;padding:1px 7px;
       border-radius:6px;margin-right:6px;background:var(--brand-ink);color:var(--brand)}
  .t-queued,.t-running{background:var(--amber-bg);color:var(--amber)}
  .t-done,.t-approval{background:var(--emerald-bg);color:var(--emerald)}
  .t-failed,.t-diagnosis{background:var(--rose-bg);color:var(--rose)}
  .agent code{font-family:ui-monospace,Menlo,monospace;font-size:.85em;
              background:#eef1f5;padding:1px 5px;border-radius:5px}
  .agent strong{font-weight:650}
  .agent .mdh{font-weight:650;margin-top:.5em}

  .inc{align-self:flex-start;max-width:80%;background:var(--emerald-bg);
       border:1px solid #bfe6d2;border-left:4px solid var(--emerald);
       border-radius:12px;padding:12px 15px;animation:rise .22s ease both}
  .inc .ihead{display:flex;align-items:center;gap:8px;font-weight:650;color:#0a5c3e}
  .inc .ihead svg{width:17px;height:17px;flex:0 0 auto}
  .inc .imeta{margin-top:6px;font-size:.82rem;color:#3d6b57;
              font-family:ui-monospace,Menlo,monospace}
  .badge{margin-left:8px;padding:1px 9px;border-radius:20px;background:#fff;
         border:1px solid #bfe6d2;color:var(--emerald);font-weight:600}
  .err{align-self:flex-start;max-width:80%;background:var(--rose-bg);
       border:1px solid #f3c2cd;color:var(--rose);border-radius:12px;padding:11px 15px}
  .approve-row{display:flex;gap:8px;margin-top:10px}
  .btn-approve,.btn-decline{padding:7px 14px;border-radius:8px;cursor:pointer;
       font-size:.9rem;font-weight:600}
  .btn-approve{background:var(--emerald);color:#fff;border:none}
  .btn-decline{background:var(--surface);color:var(--ink);border:1px solid var(--line)}
  .btn-approve:disabled,.btn-decline:disabled{opacity:.5;cursor:default}

  .typing{display:inline-flex;gap:4px;align-items:center;padding:3px 0}
  .typing i{width:6px;height:6px;border-radius:50%;background:#9aa7b8;
            animation:blink 1.3s infinite both}
  .typing i:nth-child(2){animation-delay:.18s}
  .typing i:nth-child(3){animation-delay:.36s}
  @keyframes blink{0%,80%,100%{opacity:.25;transform:translateY(0)}
                   40%{opacity:1;transform:translateY(-2px)}}

  #chips{display:flex;gap:8px;flex-wrap:wrap;padding:12px var(--gutter) 0}
  .chip{padding:7px 13px;background:var(--surface);border:1px solid var(--line);
        border-radius:20px;font-size:.82rem;cursor:pointer;color:#3a4658;
        transition:border-color .15s,color .15s}
  .chip:hover{border-color:var(--brand);color:var(--brand)}
  .chip.danger:hover{border-color:var(--rose);color:var(--rose)}

  #bar{display:flex;gap:10px;padding:14px var(--gutter)}
  #msg{flex:1;padding:12px 16px;border:1px solid var(--line);border-radius:24px;
       font-size:.95rem;background:var(--surface);color:var(--ink);outline:none;
       transition:border-color .15s,box-shadow .15s}
  #msg:focus{border-color:var(--brand);box-shadow:0 0 0 3px rgba(57,73,171,.15)}
  #send{display:grid;place-items:center;width:46px;height:46px;flex:0 0 auto;
        background:var(--brand);color:#fff;border:none;border-radius:50%;cursor:pointer;
        transition:opacity .15s,transform .05s}
  #send svg{width:19px;height:19px}
  #send:hover{opacity:.92}
  #send:active{transform:scale(.95)}
  #send:disabled{opacity:.45;cursor:default}
  @media (max-width:560px){.msg,.inc,.err{max-width:90%}}
  @media (prefers-reduced-motion:reduce){.msg,.inc,.live .dot,.typing i{animation:none}}
</style>
</head>
<body>
<header>
  <span class="logo"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7 3v5c0 4.5-3 7.6-7 9-4-1.4-7-4.5-7-9V6l7-3z"/><path d="M9 12l2.2 2.2L15 10"/></svg></span>
  <div class="htext">
    <h1>Investigation Agent</h1>
    <p>Investigate a host, watch the run, and file the incident — asynchronously on Databricks.</p>
  </div>
  <span class="live"><span class="dot"></span>Live</span>
  <button id="newchat" class="chip" onclick="newConversation()" style="margin-left:10px">New conversation</button>
</header>
<div id="thread"></div>
<div id="chips">
  <span class="chip" onclick="chip(this)">Investigate web-prod-04</span>
  <span class="chip danger" onclick="chip(this)">Run the failure demo</span>
  <span class="chip" onclick="chip(this)">What's the status?</span>
  <span class="chip" onclick="chip(this)">Why did it fail?</span>
</div>
<div id="bar">
  <input id="msg" placeholder="Message the agent…" autocomplete="off"/>
  <button id="send" onclick="send()" aria-label="Send">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4 20-7z"/></svg>
  </button>
</div>
<script>
function newSessionId(){
  try{
    const id=(crypto.randomUUID&&crypto.randomUUID())||('s-'+Date.now()+'-'+Math.random().toString(16).slice(2));
    localStorage.setItem('bw_session',id); return id;
  }catch(e){ return 's-'+Date.now(); }   // private mode / storage blocked
}
function loadSessionId(){
  try{ return localStorage.getItem('bw_session')||newSessionId(); }catch(e){ return 's-'+Date.now(); }
}
let SESSION=loadSessionId();
let currentRun="";
let currentHost="";
let pollTimer=null;
const doneRuns=new Set();
const shownIncidents=new Set();   // incident numbers already rendered (dedup)
const thread=document.getElementById('thread');
const CHECK='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M8.5 12.5l2.4 2.4L15.5 10"/></svg>';

function scrollDown(){ thread.scrollTop=thread.scrollHeight; }
function bubble(cls,text){
  const el=document.createElement('div');
  el.className=cls;
  if(text!=null) el.textContent=text;
  thread.appendChild(el);
  scrollDown();
  return el;
}
function escapeHtml(s){
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
// Minimal inline markdown on ALREADY-ESCAPED text: **bold**, *italic*, `code`.
function mdInline(s){
  return s.replace(/`([^`]+)`/g,'<code>$1</code>')
          .replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>')
          .replace(/\*([^*\n]+)\*/g,'<em>$1</em>');
}
// Render an agent message. Lines like "[obo] queried ..." become a colored
// investigation-log line (the tag conveys the run's state); other lines render
// as prose with minimal markdown. Escape FIRST — the [diagnosis] text is derived
// from job logs (untrusted), so it must never be treated as HTML.
function inlineFmt(raw){ return mdInline(escapeHtml(raw)); }
function agentHtml(text){
  return String(text).split('\n').map(function(ln){
    const m=ln.match(/^(.*?)\[([A-Za-z]+)\]\s?(.*)$/);
    if(m){
      const tag=m[2].toLowerCase();
      const rh=m[3].match(/^\s*#{1,6}\s+(.*)$/);   // heading right after a [tag]
      const rest=rh?'<strong>'+inlineFmt(rh[1])+'</strong>':inlineFmt(m[3]);
      return '<div class="logline">'+escapeHtml(m[1])+
             '<span class="tag t-'+tag+'">'+tag+'</span>'+rest+'</div>';
    }
    const hd=ln.match(/^\s*#{1,6}\s+(.*)$/);
    if(hd) return '<div class="proseline mdh">'+inlineFmt(hd[1])+'</div>';
    if(/^\s*[-*]\s+/.test(ln)) return '<div class="proseline">• '+inlineFmt(ln.replace(/^\s*[-*]\s+/,''))+'</div>';
    return '<div class="proseline">'+inlineFmt(ln)+'</div>';
  }).join('');
}
function setAgent(el,text){ el.innerHTML=agentHtml(text); scrollDown(); }
function thinking(){
  const el=bubble('msg agent',null);
  el.innerHTML='<span class="typing"><i></i><i></i><i></i></span>';
  return el;
}
function stopPolling(){ if(pollTimer){clearInterval(pollTimer);pollTimer=null;} }
// HITL: when the run completes, the agent proposes a ServiceNow incident and waits.
// Nothing is written until the human clicks Approve — the deny-by-default gate.
function attachApprovalButtons(el,d){
  const row=document.createElement('div'); row.className='approve-row';
  const ok=document.createElement('button'); ok.className='btn-approve'; ok.textContent='Approve & file';
  const no=document.createElement('button'); no.className='btn-decline'; no.textContent='Decline';
  ok.onclick=function(){doApproval('approve_incident',d.run_id||currentRun,row);};
  no.onclick=function(){doApproval('decline_incident',d.run_id||currentRun,row);};
  row.appendChild(ok); row.appendChild(no); el.appendChild(row); scrollDown();
}
async function doApproval(action,runId,row){
  row.querySelectorAll('button').forEach(function(b){b.disabled=true;});
  try{
    const r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:action,run_id:runId,host:currentHost,background:true})});
    const d=await r.json();
    setAgent(bubble('msg agent',null),(d.status_lines||[]).join('\n'));
    if(d.incident) renderIncident(d.incident);
  }catch(e){
    row.querySelectorAll('button').forEach(function(b){b.disabled=false;});
    setAgent(bubble('msg agent',null),'Network error during approval — please retry.');
  }
}
function renderIncident(i){
  // Idempotent: the same incident can arrive via both the foreground reply and
  // the background poller — render its card at most once.
  if(!i||!i.number||shownIncidents.has(i.number)) return;
  shownIncidents.add(i.number);
  const card=bubble('inc',null);
  card.innerHTML='<div class="ihead">'+CHECK+'<span>ServiceNow incident '+escapeHtml(i.number)+' filed</span></div>'+
    '<div class="imeta">caller '+escapeHtml(i.caller_id||'')+
    '<span class="badge">'+escapeHtml(i.state||'New')+'</span></div>';
  scrollDown();
}
// Background poll: quietly checks status while the run is in flight and
// auto-posts the completion when it finishes — no need to ask. Running updates
// stay silent (ask "what's the status?" to see progress on demand).
function startPolling(runId,host){
  stopPolling();
  let ticks=0;
  pollTimer=setInterval(async function(){
    ticks++;
    if(ticks>60||doneRuns.has(runId)){stopPolling();return;}
    try{
      const r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({message:'status',run_id:runId,host:host,background:true})});
      const d=await r.json();
      if(!r.ok) return;
      if(d.stage==='complete'||d.stage==='failed'){
        if(!doneRuns.has(runId)){
          doneRuns.add(runId);
          const upd=bubble('msg agent',null);
          setAgent(upd,'🔔 Update\n'+((d.status_lines||[]).join('\n')));
          if(d.awaiting_approval) attachApprovalButtons(upd,d);
          else if(d.incident) renderIncident(d.incident);
        }
        stopPolling();
      } else if(d.stage==='unknown'){ stopPolling(); }
    }catch(e){/* transient network blip — keep polling */}
  },5000);
}
function newConversation(){
  stopPolling();
  SESSION=newSessionId();
  currentRun=""; currentHost="";
  doneRuns.clear(); shownIncidents.clear();
  thread.innerHTML='';
  setAgent(bubble('msg agent',null),
    'New conversation started. Ask me to **Investigate web-prod-04** (or try **Run the failure demo**).');
  document.getElementById('msg').focus();
}
function chip(el){
  document.getElementById('msg').value=el.textContent;
  send();
}
async function send(){
  const input=document.getElementById('msg');
  const btn=document.getElementById('send');
  const text=input.value.trim();
  if(!text) return;
  bubble('msg user',text);
  input.value='';
  btn.disabled=true;
  const think=thinking();
  try{
    const r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:text,run_id:currentRun,host:currentHost,session_id:SESSION})});
    const d=await r.json();
    if(!r.ok){think.className='err';think.textContent='Something went wrong on the server. Try again.';return;}
    setAgent(think,(d.status_lines||[]).join('\n'));
    if(d.run_id&&d.stage==='queued'){currentRun=d.run_id;currentHost=d.host||currentHost;startPolling(currentRun,currentHost);}
    if(d.stage==='complete'||d.stage==='failed'){doneRuns.add(d.run_id||currentRun);stopPolling();}
    if(d.awaiting_approval){attachApprovalButtons(think,d);}
    else if(d.incident){renderIncident(d.incident);}
  }catch(e){think.className='err';think.textContent='Network error. Check your connection and retry.';}
  finally{btn.disabled=false;input.focus();}
}
document.getElementById('msg').addEventListener('keydown',function(e){
  if(e.key==='Enter'){e.preventDefault();send();}
});
window.addEventListener('load',function(){
  setAgent(bubble('msg agent',null),
    'Hi — I investigate suspicious host activity. Try **Investigate web-prod-04**, then ask for **status** anytime. I run the job on Databricks and post the incident here when it finishes.');
  document.getElementById('msg').focus();
});
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(_CHAT_HTML)


class ChatRequest(BaseModel):
    message: str = ""       # the user's chat message
    run_id: str = ""        # client-tracked current run (for status queries)
    host: str = ""          # client-tracked host (for status queries / submit default)
    session_id: str = ""    # client conversation id (localStorage) -> memory thread
    background: bool = False  # true for silent poller pings: no memory thread (may generate a one-time failure diagnosis)
    action: str = ""        # deterministic UI action: "approve_incident" / "decline_incident" (no LLM)


@app.post("/chat")
def chat(body: ChatRequest, request: Request):
    """Conversational entry point (LLM agent).

    Runs the LangGraph agent (route_and_run) with short-term memory; on any
    exception (e.g. the model endpoint is down) returns a graceful error — there
    is no deterministic keyword fallback.  Returns:
    {"status_lines": [...], "incident": {...}|None, "run_id": "...",
     "host": "...", "stage": "...", "intent": "...", "user_id": "..."}
    """
    # Resolve user identity (OBO via x-forwarded-email).
    user_id = user_identity(dict(request.headers))
    if user_id == "unknown":
        # Deployed App always injects forwarded headers; this path is local-only.
        user_id = os.environ.get("LOCAL_USER", "local-test-user@example.com")
    user_token = request.headers.get("x-forwarded-access-token")

    # Deterministic HITL actions from the UI (Approve / Decline buttons). No LLM,
    # no memory — the human's click is the gate before any ServiceNow write.
    if body.action in ("approve_incident", "decline_incident"):
        store_conn = lakebase_conn()
        try:
            store = PostgresStatusStore(store_conn)
            resp = (approve_incident if body.action == "approve_incident"
                    else decline_incident)(body.run_id, user_id, store)
        except Exception as exc:
            logger.warning("approval action failed (%s); returning graceful error", exc)
            resp = {"run_id": body.run_id, "stage": "error", "incident": None,
                    "status_lines": ["[error] the approval action is temporarily unavailable — please retry"]}
        finally:
            store_conn.close()
        resp["intent"] = "approval"
        resp["user_id"] = user_id
        return JSONResponse(resp)

    # Silent background poll: answer status directly, never touch the LLM or the
    # memory thread (keeps the conversation to the user's typed turns).
    if body.background:
        store_conn = lakebase_conn()
        try:
            store = PostgresStatusStore(store_conn)
            resp = check_status(body.run_id, body.host, user_id, store)
            resp["intent"] = "status"
            resp["user_id"] = user_id
            return JSONResponse(resp)
        finally:
            store_conn.close()

    store_conn = lakebase_conn()
    cp_conn = None
    try:
        store = PostgresStatusStore(store_conn)
        checkpointer = None
        thread_id = _compose_thread_id(user_id, body.session_id)
        # Turn tracking (observability only): open a turn now, close it when the
        # request finishes. A crash that never returns leaves it 'open', and the
        # scheduled sweeper job marks it 'abandoned' past its TTL. open_turn /
        # close_turn are best-effort in the store (rolled back + logged on failure),
        # so they never raise here or break the chat turn.
        turn_id = uuid.uuid4().hex
        store.open_turn(turn_id, thread_id, user_id)
        try:
            checkpointer, cp_conn = lakebase_checkpointer()
        except Exception as exc:      # one boundary guard: memory optional
            logger.warning("checkpointer unavailable (%s); stateless invoke", exc)
        try:
            resp = route_and_run(body.message, body.run_id, body.host,
                                 user_id, user_token, store,
                                 checkpointer=checkpointer, thread_id=thread_id)
        except Exception as exc:
            logger.warning("agent path failed (%s); returning graceful error", exc)
            resp = {"intent": "error", "stage": "error", "run_id": body.run_id,
                    "host": body.host,
                    "status_lines": ["[error] the agent is temporarily unavailable — please try again"]}
        store.close_turn(turn_id)     # request finished (success or graceful error)
        resp["user_id"] = user_id
        return JSONResponse(resp)
    finally:
        store_conn.close()
        if cp_conn is not None:
            cp_conn.close()
