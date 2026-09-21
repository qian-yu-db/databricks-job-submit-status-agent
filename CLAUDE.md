# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Databricks-hosted **LLM investigation agent** (demo). A FastAPI Databricks App (`agent/app.py`) fronts a LangGraph ReAct agent that investigates suspicious-login hosts by triggering long-running Databricks Jobs, streaming status from Lakebase (Postgres), diagnosing failures, and filing ServiceNow incidents through a second Databricks App that serves an MCP server. Deployed via Declarative Automation Bundles (DAB).

## Commands

- **Deps:** `uv sync` (Python 3.11+, uv-managed). Run everything with `uv run`.
- **Tests:** `uv run pytest -q`. Single test: `uv run pytest tests/test_llm_agent.py::test_name -q`. The suite is fast/offline — it monkeypatches Lakebase, the LLM, and the Jobs API; no workspace needed.
- **Routing eval:** `eval/routing_eval.py` is the one thing that hits the **real** model (with mocked tools) — don't run it in the normal test loop.
- **Deploy (DAB):** `databricks bundle deploy --profile <your-profile>`, then `databricks bundle run agent --profile <your-profile>` (and `... run servicenow_mcp` for the MCP app). Target `dev` is the default.
- If `git` fails with an Xcode-license error in this environment, use `/Library/Developer/CommandLineTools/usr/bin/git` (or prepend that dir to `PATH`).

## Deploy prerequisites (must exist before the app runs — NOT created by app code)

Per environment, admin-owned:
- Lakebase **`workflow_status`** table (`lakebase/schema.sql`); the app SP needs DML on it.
- Lakebase **`agent_config.jobs`** registry table — **admin-owned, app SP has SELECT only** (so the app can't rewrite its own allowlist). One row per workflow with its Databricks `job_id`. **job_ids are workspace-specific — reseed the rows for each environment.**
- App SP grants: `CAN_MANAGE_RUN` on each job, `CAN_USE` on the ServiceNow MCP app, DML on `workflow_status` and `turns` (the sweeper SP needs `SELECT`/`UPDATE` on `turns`), `CAN_EDIT` on the MLflow experiment (`MLFLOW_EXPERIMENT`), `EXECUTE` on the `system.ai.claude-sonnet-5` model service (the agent's LLM routes through Unity AI Gateway — see Architecture; without it every model call 403s).
- **Unity AI Gateway V2 enabled** in the workspace, with the `system.ai.claude-sonnet-5` model service reachable through it — the agent's model calls route through the gateway and there is **no fallback** to a raw serving endpoint. Like `job_id`s, this is a per-environment capability: verify it in each workspace (and grant the app SP `EXECUTE` on the model service, above).
- **Recreating the app mints a new service principal** — every grant above must be re-applied to it.

## Architecture (the parts that span multiple files)

**Two apps, one bundle** (`databricks.yml` + `resources/apps.yml`), both deployed **from the repo root** (`source_code_path: ..`) so `agent`, `common`, `servicenow_mcp`, and `jobs` import as sibling packages. The **deployed apps install from `requirements.txt`** (pip), pinned to exact versions, from **public PyPI**. The agent app installs from the **root** `requirements.txt` (its `source_code_path` is the repo root); the MCP app from `servicenow_mcp/requirements.txt`. Apps support both pip and uv (`pyproject.toml` + `uv.lock`); this repo uses pip because **some corporate/internal workspaces route uv's installs through a private PyPI proxy that Apps can't reach** (deploys time out), whereas pip from public PyPI always works. If uv works on your workspace, you can use it instead. To refresh pins: `uv pip compile requirements.txt --python-version 3.11`. `uv`/`pyproject.toml` remain the **local-dev** tool. Jobs are separate: deps stay inline in `resources/jobs.yml` (serverless env spec).
- `agent` → `job-status-agent`: the FastAPI orchestrator + inline chat UI.
- `servicenow_mcp` → `servicenow-mcp`: a FastMCP streamable-HTTP MCP server (stub ServiceNow backend).

**Request flow** (`agent/app.py` `/chat`):
1. Identity from `x-forwarded-email` / `x-forwarded-access-token` (Databricks Apps OBO headers).
2. `background: true` pings answer via `check_status` directly (no LLM, no memory) — that's the browser's status poller.
3. Everything else goes to `route_and_run` (`agent/llm_agent.py`): a ReAct agent built with `create_agent` (from `langchain.agents`; the successor to the deprecated LangGraph `create_react_agent` — uses `system_prompt=` not `prompt=`) over Claude Sonnet 5, referenced as its governed UC model service `system.ai.claude-sonnet-5` and called with `use_ai_gateway=True` so every LLM call routes through **Unity AI Gateway** (usage tracking, payload logging, guardrails, rate limits; the SP needs `EXECUTE` on the model service — no fallback to the raw serving endpoint, by design), with three governed tools (`investigate` / `check_status` / `diagnose_run`). On any agent failure it returns a graceful error — there is **no deterministic keyword fallback** (that path was removed; the LLM agent is the only mode).

**Governed job registry** (`agent/job_registry.py` + `agent_config.jobs`): the agent chooses a `job_key`; trusted server code (`resolve_job`) validates it against the admin-owned table and resolves the real `job_id` (the LLM never handles a raw job id). Two workflows today, **each its own Databricks job**: `investigation` and `failure_demo`.

**Jobs** (`resources/jobs.yml`, `jobs/*.py`): serverless `spark_python_task`s triggered by the app via `jobs.run_now` (as the SP), which captures the Databricks run_id. `investigation_job.py` runs the real investigation and streams stages to Lakebase; `failure_demo_job.py` just raises a realistic `AnalysisException` so the diagnosis path has a genuine failed run to explain.

**State & memory** (`agent/status_store.py`, `common/`): `workflow_status` (keyed by `run_id`) is the per-run status/findings/incident store — job-agnostic, so any number of workflows share it. Short-term conversational memory is a LangGraph `PostgresSaver` checkpointer in a dedicated **SP-owned `agent_memory` schema**, keyed by `thread_id = user_id:session_id` (browser `localStorage` session id).

**Identity model** (spans `obo_investigate`, `launch_job`, `_call_servicenow_mcp` in `agent/app.py`): real token-level **OBO is used only for the SQL warehouse query** (forwarded user token, `auth_type="pat"`, Unity Catalog-enforced). There is **no Jobs-API OBO scope**, so jobs are triggered/polled as the **app SP**, and the MCP call is SP-authenticated too. The user's identity is passed as a **parameter** (job `--user-id`, MCP `caller_id`) for attribution, not enforcement.

**Failure diagnosis** (`agent/diagnosis.py`): `check_status` detects a failed run (the job's own `stage="failed"`, or the Jobs API `result_state`), then a tool-less LLM call grounds a diagnosis on the run's error log. `claim_diagnosis_slot`/`release_diagnosis_slot` ensure the LLM fires once when a foreground "why did it fail?" races the poller, and a transient failure is never cached (so it retries).

**Tracing** (`agent/tracing.py`): `enable_tracing()` logs LangGraph traces to the workspace MLflow experiment named by `MLFLOW_EXPERIMENT`; unset (local dev) falls back to MLflow's local store. Traces are workspace experiment assets, not Unity Catalog objects.

## Conventions / gotchas

- **Databricks CLI profile** — pass it explicitly (`--profile <your-profile>`); the bundle doesn't pin one. Never auto-select a profile.
- **`claude-sonnet-5` rejects the `temperature` param** — `get_model()` sets none. Its `.content` may be a list of blocks (or a JSON-serialized string after a checkpointer round-trip); `_flatten_content` normalizes both, so never render `.content` raw.
- **Lakebase connections** are per-request with a fresh 1h OAuth token (`common/lakebase.py`, `agent/app.py: lakebase_conn`); on macOS pass `hostaddr` (resolved via `dig`) — Python DNS fails on the long endpoint hostname.
- **DAB dev mode** uses `presets.name_prefix: ""` — do not let it prefix app/job names (strict Apps naming rules).

A guided walkthrough of the codebase lives in [`docs/`](docs/README.md).
