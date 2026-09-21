"""Demo-only Databricks job (its own job_id) that deliberately fails, so the
app's failure-diagnosis path has a realistic failed run to explain.

It raises immediately and touches nothing else — no Lakebase write, no psycopg
import (importing the native libpq driver only to write a status was crashing the
serverless kernel and polluting the diagnosis). The app detects the failed run
via the Jobs API (`_databricks_result_state`) and diagnoses from the run's error
log, which now contains only this clean Spark/Unity Catalog error.
"""

_ERROR = (
    "AnalysisException: [TABLE_OR_VIEW_NOT_FOUND] Table or view not found: "
    "main.job_agent_demo.auth_events_stg"
)


def run(*args, **kwargs) -> None:
    """Raise the simulated failure. Accepts (and ignores) any launch args so it is
    call-compatible with the app's launch_job python_params."""
    raise RuntimeError(_ERROR)


def main() -> None:
    run()


if __name__ == "__main__":
    main()
