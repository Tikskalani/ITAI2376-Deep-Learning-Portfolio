-- Atlas Memory Schema -- Migration 013_workflow_runs
-- R4: durable, resumable, approval-aware workflow runs.
--
--   workflow_runs   one row per execution of a workflows/<name>.yml recipe
--   workflow_steps  one row per step of a run; state is persisted after
--                   every step so a crash or restart resumes where it was
--
-- A step that waits (an approval card, a wait_until) leaves the run in
-- status 'waiting'; the engine's scheduler job picks it up when the wait
-- is over. Idempotent.

create table if not exists workflow_runs (
  id            uuid primary key default gen_random_uuid(),
  name          text not null,
  version       integer not null default 1,
  status        text not null default 'pending'
                check (status in ('pending', 'running', 'waiting', 'done', 'failed', 'cancelled')),
  trigger       text not null default 'manual',
  input         jsonb not null default '{}'::jsonb,
  current_step  integer not null default 0,
  project_id    uuid references projects(id) on delete set null,
  final_output  text,
  error         text,
  started_at    timestamptz,
  finished_at   timestamptz,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

create index if not exists workflow_runs_status_idx on workflow_runs (status, updated_at);

create table if not exists workflow_steps (
  id               uuid primary key default gen_random_uuid(),
  run_id           uuid not null references workflow_runs(id) on delete cascade,
  index            integer not null,
  type             text not null
                   check (type in ('agent', 'approval', 'wait_until', 'condition', 'notify')),
  status           text not null default 'pending'
                   check (status in ('pending', 'running', 'waiting', 'done', 'failed', 'skipped')),
  attempts         integer not null default 0,
  input            jsonb not null default '{}'::jsonb,
  output           jsonb not null default '{}'::jsonb,
  waiting_on       jsonb,
  error            text,
  started_at       timestamptz,
  finished_at      timestamptz,
  unique (run_id, index)
);
