# Packaging: make sibling repo packages (agent, common) importable when this
# file runs as a serverless spark_python_task. The whole bundle is synced next
# to it, but `__file__` is undefined in the serverless exec kernel, so locate the
# repo root by walking cwd / sys.path for the dir that holds agent/ and common/.
import os, sys, time


def _ensure_repo_root_on_path() -> None:
    starts = []
    try:
        starts.append(os.path.dirname(os.path.abspath(__file__)))
    except NameError:
        pass
    starts.append(os.getcwd())
    starts.extend(p for p in sys.path if p)
    checked = set()
    for start in starts:
        d = os.path.abspath(start)
        for _ in range(8):
            if d in checked:
                break
            checked.add(d)
            if os.path.isdir(os.path.join(d, "agent")) and os.path.isdir(os.path.join(d, "common")):
                if d not in sys.path:
                    sys.path.insert(0, d)
                return
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    sys.stderr.write(
        f"[investigation_job] repo root (agent/+common/) not found. "
        f"cwd={os.getcwd()} sys.path={sys.path}\n"
    )


_ensure_repo_root_on_path()

from datetime import datetime, timezone
from agent.stages import investigation_stages
from agent.investigation import investigate
from agent.status_store import PostgresStatusStore
from common.models import WorkflowStatus

def run(
    run_id: str,
    thread_id: str,
    user_id: str,
    host: str,
    delay: float,
    store,
    *,
    execute_sql=None,
    table: str | None = None,
) -> None:
    def now(): return datetime.now(timezone.utc)
    for stage, detail in investigation_stages():
        result = investigate(host, execute_sql=execute_sql, table=table).__dict__ if stage == "complete" else None
        store.upsert(WorkflowStatus(run_id=run_id, thread_id=thread_id, user_id=user_id,
                                    stage=stage, detail=detail, result=result, updated_at=now()))
        if stage != "complete":
            time.sleep(delay)

def main() -> None:
    # Parameters arrive as CLI argv from Databricks Job `python_params` (not env vars).
    # The app's trigger_job() passes: --run-id X --thread-id Y --user-id Z --host H
    # Legacy env-var fallback retained for local/script invocations.
    import argparse
    parser = argparse.ArgumentParser(description="Investigation job")
    parser.add_argument("--run-id", default=os.environ.get("JOB_RUN_ID"))
    parser.add_argument("--thread-id", default=os.environ.get("JOB_THREAD_ID"))
    parser.add_argument("--user-id", default=os.environ.get("JOB_USER_ID"))
    parser.add_argument("--host", default=os.environ.get("JOB_HOST"))
    parser.add_argument("--delay", type=float,
                        default=float(os.environ.get("JOB_STAGE_DELAY_SECONDS", "8")))
    # Non-secret infra config: passed as argv by the app (or defaulted for
    # scheduled/manual runs). Serverless tasks can't set arbitrary env vars.
    parser.add_argument("--warehouse-id", default=os.environ.get("WAREHOUSE_ID"))
    parser.add_argument("--auth-table", default=os.environ.get("AUTH_TABLE"))
    args = parser.parse_args()
    for attr in ("run_id", "thread_id", "user_id", "host"):
        if getattr(args, attr) is None:
            raise ValueError(f"Missing required parameter: --{attr.replace('_','-')}")
    p = {"run_id": args.run_id, "thread_id": args.thread_id,
         "user_id": args.user_id, "host": args.host}
    delay = args.delay
    execute_sql = None
    table = None
    if args.warehouse_id and args.auth_table:
        from databricks.sdk import WorkspaceClient
        from agent.investigation import sdk_sql_executor
        w = WorkspaceClient()
        execute_sql = sdk_sql_executor(w, args.warehouse_id)
        table = args.auth_table
    from common.lakebase import connect
    with connect() as conn:
        run(**p, delay=delay, store=PostgresStatusStore(conn), execute_sql=execute_sql, table=table)

if __name__ == "__main__":
    main()
