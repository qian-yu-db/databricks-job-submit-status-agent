"""Governed job registry: reads the admin-owned, app-read-only
agent_config.jobs table (SP has SELECT) and resolves a job_key -> job_id.

The registry is a deployment prerequisite; there is no hardcoded fallback.
A short-TTL in-process cache rides out transient Lakebase blips."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_TABLE = "agent_config.jobs"
_TTL = 60.0
_CACHE: dict = {"data": None, "ts": 0.0}
_LOCK = threading.Lock()


class RegistryError(Exception):
    """Bad/unknown/disabled job_key or invalid params (a user-facing 4xx-style error)."""


class RegistryUnavailable(Exception):
    """The registry could not be read and no cached copy exists."""


@dataclass
class JobSpec:
    job_key: str
    display_name: str
    description: str
    databricks_job_id: int
    param_schema: dict = field(default_factory=dict)
    fixed_params: dict = field(default_factory=dict)
    requires_approval: bool = False
    allowed_groups: list = field(default_factory=list)
    enabled: bool = True


def load_jobs(conn) -> dict[str, JobSpec]:
    """Read enabled rows from agent_config.jobs into {job_key: JobSpec}."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT job_key, display_name, description, databricks_job_id, "
            f"param_schema, fixed_params, requires_approval, allowed_groups, enabled "
            f"FROM {_TABLE} WHERE enabled = true ORDER BY job_key"
        )
        rows = cur.fetchall()
    out: dict[str, JobSpec] = {}
    for r in rows:
        # rows are dicts (dict_row) or tuples; support both.
        g = (lambda k, i: r[k] if isinstance(r, dict) else r[i])
        out[g("job_key", 0)] = JobSpec(
            job_key=g("job_key", 0), display_name=g("display_name", 1),
            description=g("description", 2), databricks_job_id=int(g("databricks_job_id", 3)),
            param_schema=g("param_schema", 4) or {}, fixed_params=g("fixed_params", 5) or {},
            requires_approval=bool(g("requires_approval", 6)),
            allowed_groups=g("allowed_groups", 7) or [], enabled=bool(g("enabled", 8)))
    return out


def _open_conn():
    from agent.app import lakebase_conn
    from psycopg.rows import dict_row
    return lakebase_conn(row_factory=dict_row)


def get_registry(force: bool = False) -> dict[str, JobSpec]:
    """Cached registry snapshot. Serves the last-good copy within the TTL and on a
    transient read failure; raises RegistryUnavailable only when there is no cache."""
    now = time.time()
    if not force and _CACHE["data"] is not None and now - _CACHE["ts"] < _TTL:
        return _CACHE["data"]
    with _LOCK:
        if not force and _CACHE["data"] is not None and time.time() - _CACHE["ts"] < _TTL:
            return _CACHE["data"]
        try:
            conn = _open_conn()
            try:
                data = load_jobs(conn)
            finally:
                conn.close()
        except Exception as exc:
            if _CACHE["data"] is not None:
                logger.warning("registry refresh failed (%s); serving cached copy", exc)
                return _CACHE["data"]
            raise RegistryUnavailable(str(exc))
        _CACHE["data"] = data
        _CACHE["ts"] = time.time()
        return data


def resolve_job(job_key: str, params: dict, registry: dict[str, JobSpec]) -> tuple[int, dict]:
    """Deterministic resolution: validate the key is present + enabled, merge
    fixed_params over caller params (server-forced wins), verify required params,
    return (databricks_job_id, merged_params). Raises RegistryError otherwise."""
    spec = registry.get(job_key)
    if spec is None or not spec.enabled:
        raise RegistryError(f"no workflow '{job_key}' is configured")
    merged = {**(params or {}), **(spec.fixed_params or {})}
    for name, decl in (spec.param_schema or {}).items():
        if decl.get("required") and merged.get(name) in (None, ""):
            raise RegistryError(f"workflow '{job_key}' requires a '{name}'")
    return spec.databricks_job_id, merged
