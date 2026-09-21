from common.models import Findings
from agent import stages

# host is bound via a named parameter (:host); {table} is a trusted config value.
SUSPICIOUS_LOGINS_SQL = """
SELECT source_asn, user_id, event_type,
       count(*) AS events, count(DISTINCT source_ip) AS distinct_ips,
       min(event_time) AS first_seen, max(event_time) AS last_seen
FROM {table}
WHERE host = :host
  AND source_asn NOT LIKE 'AS-CORP%'
  AND hour(event_time) BETWEEN 0 AND 5
GROUP BY source_asn, user_id, event_type
ORDER BY events DESC
"""


def query_suspicious_logins(host: str, *, table: str, execute_sql) -> list[dict]:
    """execute_sql(sql: str, params: dict) -> list[dict]. Returns the grouped rows."""
    return execute_sql(SUSPICIOUS_LOGINS_SQL.format(table=table), {"host": host})


def build_findings_from_rows(host: str, rows: list[dict]) -> Findings:
    if not rows:
        return Findings(
            host=host,
            summary=f"No suspicious off-hours external logins found on {host}",
            severity="low",
            indicators=[],
        )

    def n(r):
        return int(r["events"])

    failed_rows = [r for r in rows if r["event_type"] == "login_failed"]
    top = max(failed_rows if failed_rows else rows, key=n)
    asn = top["source_asn"]
    user = top["user_id"]
    actor_rows = [r for r in rows if r["source_asn"] == asn and r["user_id"] == user]
    failed = sum(n(r) for r in actor_rows if r["event_type"] == "login_failed")
    success = sum(n(r) for r in actor_rows if r["event_type"] == "login_success")
    ips = max(int(r["distinct_ips"]) for r in actor_rows)
    if success > 0 and failed >= 10:
        severity = "high"
    elif success > 0:
        severity = "medium"
    else:
        severity = "low"
    summary = (
        f"{failed} failed then {success} successful off-hours logins on {host} "
        f"from {asn} across {ips} source IPs targeting {user}"
    )
    indicators = [asn, f"user:{user}", f"failed:{failed}", f"success:{success}", f"distinct_ips:{ips}"]
    return Findings(host=host, summary=summary, severity=severity, indicators=indicators)


def investigate(host: str, *, execute_sql=None, table: str | None = None) -> Findings:
    """Real query when execute_sql+table are provided; otherwise the mock fallback (spec §7)."""
    if execute_sql is None or table is None:
        return stages.build_findings(host)
    rows = query_suspicious_logins(host, table=table, execute_sql=execute_sql)
    return build_findings_from_rows(host, rows)


def sdk_sql_executor(workspace_client, warehouse_id: str):
    """Return an execute_sql(sql, params) backed by the Databricks SQL Statement Execution API."""
    from databricks.sdk.service.sql import StatementParameterListItem

    def execute(sql: str, params: dict) -> list[dict]:
        resp = workspace_client.statement_execution.execute_statement(
            warehouse_id=warehouse_id,
            statement=sql,
            wait_timeout="30s",
            parameters=[StatementParameterListItem(name=k, value=str(v)) for k, v in params.items()],
        )
        cols = [c.name for c in resp.manifest.schema.columns]
        data = resp.result.data_array or []
        return [dict(zip(cols, row)) for row in data]

    return execute
