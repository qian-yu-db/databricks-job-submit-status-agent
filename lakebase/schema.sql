-- lakebase/schema.sql
CREATE TABLE IF NOT EXISTS workflow_status (
  run_id      TEXT PRIMARY KEY,
  thread_id   TEXT NOT NULL,
  user_id     TEXT NOT NULL,
  stage       TEXT NOT NULL,
  detail      TEXT,
  result      JSONB,
  updated_at  TIMESTAMPTZ NOT NULL,
  job_run_id  BIGINT
);
-- Idempotent migration: add job_run_id to a workflow_status table that predates
-- this column (CREATE TABLE IF NOT EXISTS above is a no-op on an existing table).
ALTER TABLE workflow_status ADD COLUMN IF NOT EXISTS job_run_id BIGINT;
CREATE TABLE IF NOT EXISTS turns (
  turn_id     TEXT PRIMARY KEY,
  thread_id   TEXT NOT NULL,
  user_id     TEXT NOT NULL,
  state       TEXT NOT NULL DEFAULT 'open',   -- open | closed | abandoned
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_turns_state_created ON turns (state, created_at);
