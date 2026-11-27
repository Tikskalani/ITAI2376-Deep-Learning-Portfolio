-- 021: the tool ledger, so an outward action never happens twice.
--
-- Agents are retried: the dispatcher tries once more after a failed run,
-- and a workflow step is retried up to three times. Each retry is a fresh
-- agent run that may call the same gated tool (send this email, submit this
-- application) again. tool_ledger remembers every gated call by
--   key = sha256(scope | tool | canonical input)
-- where scope is the unit of work being retried (a workflow step, one
-- dispatch, or one agent run). A retry inside the same scope finds the row
-- and gets the earlier result instead of acting again.
--
--   running   claimed; the action may be in flight (a crash leaves it here,
--             and a retry is refused rather than risk a second send)
--   done      ran; `result` holds what it returned
--   failed    raised NotStarted (failed before acting); a retry may claim it
--   pending   waiting on an approval card; a retry does not raise a second card
--   rejected  Trilok said no; a retry does not ask again
--   expired   the card expired undecided; a retry may ask again

create table if not exists tool_ledger (
  key          text primary key,
  scope        text not null,
  tool         text not null,
  agent_name   text not null,
  status       text not null default 'running'
               check (status in ('running', 'done', 'failed', 'pending', 'rejected', 'expired')),
  input        jsonb not null default '{}'::jsonb,
  result       jsonb,
  episode_id   uuid,
  approval_id  uuid,
  attempts     integer not null default 1,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

create index if not exists tool_ledger_scope_idx on tool_ledger (scope, created_at);
