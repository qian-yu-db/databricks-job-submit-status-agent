"""Lakebase connection helper for serverless jobs.

Serverless jobs (unlike Databricks Apps) do not get PG* env injected by an
attached Lakebase resource, and a static DSN token would expire (1h TTL).
So jobs generate a fresh short-lived OAuth credential via the SDK at run time.

Resolution order:
  1. LAKEBASE_DSN env  -> use verbatim (local/dev override, e.g. a psql DSN)
  2. otherwise         -> generate a credential for LAKEBASE_ENDPOINT and
                          connect using host/user/db derived from env or the
                          endpoint record + the job's own identity.
"""
from __future__ import annotations

import os

DEFAULT_ENDPOINT = "projects/job-agent/branches/production/endpoints/primary"
DEFAULT_DATABASE = "databricks_postgres"


def connect(autocommit: bool = False):
    """Return a fresh psycopg3 connection to Lakebase (caller closes it)."""
    import psycopg

    dsn = os.environ.get("LAKEBASE_DSN")
    if dsn:
        return psycopg.connect(dsn, autocommit=autocommit)

    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient()
    endpoint = os.environ.get("LAKEBASE_ENDPOINT", DEFAULT_ENDPOINT)
    token = w.postgres.generate_database_credential(endpoint).token

    host = os.environ.get("PGHOST")
    if not host:
        host = w.postgres.get_endpoint(endpoint).status.hosts.host
    user = os.environ.get("PGUSER") or w.current_user.me().user_name
    dbname = os.environ.get("PGDATABASE", DEFAULT_DATABASE)
    port = int(os.environ.get("PGPORT", "5432"))

    return psycopg.connect(
        host=host, dbname=dbname, user=user, password=token,
        port=port, sslmode="require", autocommit=autocommit,
    )
