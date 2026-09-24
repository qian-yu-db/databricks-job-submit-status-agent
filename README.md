# LLM Investigation Agent (Databricks)

A Databricks-hosted **LLM investigation agent**: a FastAPI Databricks App fronts a
LangGraph ReAct agent that investigates suspicious-login hosts by triggering
long-running Databricks Jobs, streaming status from Lakebase (Postgres),
diagnosing failures, and filing ServiceNow incidents through a second Databricks
App that serves an MCP server. Deployed via Declarative Automation Bundles (DAB).

> Working with this codebase in Claude Code? See **[CLAUDE.md](CLAUDE.md)** for the
> deeper architecture notes and gotchas.

## What it does

- **Conversational investigations.** Ask the agent to investigate a host; it queries
  the auth logs **as you** (Unity Catalog–enforced, on-behalf-of), launches a
  serverless investigation Job, and returns a run id without blocking.
- **Status on demand + async updates.** Ask "what's the status?" anytime; the chat UI
  also polls in the background and posts the result when the run finishes.
- **Governed workflows.** The agent selects a workflow by `job_key`; trusted server
  code resolves it to a real Databricks `job_id` from an admin-owned registry table
  (the model never handles a raw job id).
- **Failure diagnosis.** When a run fails, the agent produces a grounded diagnosis
  from the run's error log.
- **Short-term memory.** Conversations are checkpointed in Lakebase, so the agent
  recalls the runs it started earlier in the same session.
- **Incident filing.** Completed investigations file a ServiceNow incident (via the
  MCP app), attributed to the requesting user.

## Architecture (short version)

Two Databricks Apps in one bundle, both deployed from the repo root so the
`agent`, `common`, `servicenow_mcp`, and `jobs` packages import as siblings:

- **`agent`** — FastAPI orchestrator + inline chat UI. `/chat` runs the LangGraph
  agent (`databricks-claude-sonnet-5`) with three governed tools
  (`investigate` / `check_status` / `diagnose_run`).
- **`servicenow_mcp`** — a FastMCP streamable-HTTP MCP server (stub ServiceNow backend).

State lives in Lakebase: a `workflow_status` table (per-run status/findings/incident,
keyed by `run_id`) and a LangGraph `PostgresSaver` checkpointer (short-term memory,
keyed by `user_id:session_id`). Investigations run as serverless `spark_python_task`
Jobs. See [CLAUDE.md](CLAUDE.md) for the identity model, the job registry, and the
Lakebase/OBO details.

## Requirements

- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).
- Databricks CLI, a workspace with Lakebase, a SQL warehouse, and a
  `databricks auth` profile.

## Setup & test

```bash
uv sync                 # install dependencies
uv run pytest -q        # run the test suite (offline — mocks Lakebase, LLM, Jobs API)
```

Run a single test:

```bash
uv run pytest tests/test_llm_agent.py::test_name -q
```

## Deploy

Deployed with Declarative Automation Bundles (target `dev` is the default):

```bash
databricks bundle deploy --profile <your-profile>
databricks bundle run agent --profile <your-profile>
databricks bundle run servicenow_mcp --profile <your-profile>
```

This repo supports **two deploy paths** — use whichever fits:

- **CLI / bundle** (above): `databricks bundle deploy` then `bundle run <app>` ships both
  apps and the jobs from one bundle. Each app's `command`/`env` comes from the `config:`
  block in `resources/apps.yml`.
- **Manual / UI (source-only)**: deploy each app from the workspace UI, one at a time, from
  its **own** source path — `job-status-agent` from the **repo root** (reads the root
  `app.yaml`) and `servicenow-mcp` from **`servicenow_mcp/`** (reads `servicenow_mcp/app.yaml`).
  Attach the Lakebase resource in the app's UI and fill in its env.

Both produce the same running apps: the CLI path reads the bundle `config:` block and ignores
`app.yaml`; the UI path reads `app.yaml` and ignores the bundle. **Keep each `app.yaml` in sync
with its matching `config:` block** so the two behave identically. Full manual-deploy steps:
[`docs/01-setup.md`](docs/01-setup.md) → *Manual (UI) deploy — one app at a time*.

**Prerequisites that must exist before the app runs (not created by app code):**

- The Lakebase `workflow_status` and `turns` tables (`lakebase/schema.sql`).
- An admin-owned `agent_config.jobs` registry table (app service principal granted
  `SELECT` only), seeded with one row per workflow and its Databricks `job_id`.
  **Job ids are workspace-specific — reseed per environment.**
- The Unity Catalog `auth_events` table (your `AUTH_TABLE`) — the login-event data the
  agent queries as the user; **create and seed it yourself** (DDL + demo rows in
  [`docs/01-setup.md`](docs/01-setup.md)).
- **Unity AI Gateway V2** enabled, with `system.ai.claude-sonnet-5` reachable through it.
- **Grants** (full list + runnable SQL in [`docs/01-setup.md`](docs/01-setup.md) → *Grants — who
  needs what*): the app service principal needs `EXECUTE` on `system.ai.claude-sonnet-5`,
  `CAN_MANAGE_RUN` on each job, `CAN_USE` on the MCP app, `CAN_EDIT` on the MLflow experiment,
  DML on `workflow_status`/`turns`, and `SELECT` on `agent_config.jobs`; the sweeper's run-as
  identity needs `SELECT`/`UPDATE` on `turns`; and end **users** need `SELECT` on `auth_events`
  (queried on-behalf-of). Recreating an app mints a new service principal, so all grants must be
  re-applied.

## Layout

```
agent/            FastAPI app, LangGraph agent, job registry, OBO, diagnosis, tracing
servicenow_mcp/   MCP server (stub ServiceNow backend)
jobs/             serverless spark_python_task job scripts
common/           Lakebase connection + shared data models
lakebase/         SQL schema
resources/        DAB resource definitions (apps, jobs)
eval/             real-model routing eval (mocked tools)
tests/            pytest suite
docs/             guided codebase walkthrough (start at docs/README.md)
```

## Developing with Claude Code

This repo pairs well with [Claude Code](https://www.anthropic.com/claude-code) —
`CLAUDE.md` gives it the architecture and conventions it needs. For Databricks-specific
help while building, deploying, and testing agents, install the official **Databricks
Claude Code plugin** (Databricks Asset Bundles, Apps, Lakebase, and Model Serving
skills), and see Databricks' agent app templates for reusable scaffolds:

- Databricks agent app templates — https://github.com/databricks/app-templates
- Author an agent and deploy on Databricks Apps — https://docs.databricks.com/aws/en/generative-ai/agent-framework/author-agent

## License & security

See [`LICENSE`](LICENSE) for terms. To report a security issue, see [`SECURITY.md`](SECURITY.md).
