-- Atlas Memory Schema -- Migration 011_senses
-- R2: projects become the unit of organization, signals become the input.
--
--   projects         one row per thing Trilok is working on; replaces the
--                    priority list hardcoded in the CEO prompt
--   signals          every inbound event (GitHub, Canvas, Gmail later),
--                    idempotent on (source, external_id); triage fills in
--                    project, urgency, importance, deadline
--   connector_state  per-source cursor so a restart never re-reads history
--   approval_policy  tool-name patterns -> auto | approve | deny; the
--                    runtime checks it before running any tool
--   llm_calls        usage for one-shot model calls (triage, extraction)
--                    so they count against the daily cap
--
-- Idempotent: every statement is create-if-not-exists or on-conflict-do-nothing.

create table if not exists projects (
  id               uuid primary key default gen_random_uuid(),
  slug             text not null unique,
  name             text not null,
  kind             text not null default 'client'
                   check (kind in ('client', 'own', 'hobby', 'personal', 'college', 'atlas')),
  status           text not null default 'active'
                   check (status in ('active', 'waiting', 'parked', 'done')),
  priority         smallint not null default 3 check (priority between 1 and 5),
  objective        text,
  next_action      text,
  next_action_due  date,
  owner_agent      text not null default 'ceo',
  repos            text[] not null default '{}',
  last_signal_at   timestamptz,
  last_touched_at  timestamptz,
  metadata         jsonb not null default '{}'::jsonb,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);

create index if not exists projects_status_priority_idx
  on projects (status, priority);

create table if not exists signals (
  id             uuid primary key default gen_random_uuid(),
  source         text not null,
  external_id    text not null,
  occurred_at    timestamptz not null,
  actor          text,
  subject        text not null,
  body           text not null default '',
  url            text,
  project_id     uuid references projects(id) on delete set null,
  entity_ids     uuid[] not null default '{}',
  urgency        smallint not null default 0 check (urgency between 0 and 3),
  importance     smallint not null default 0 check (importance between 0 and 3),
  triage_status  text not null default 'new'
                 check (triage_status in ('new', 'triaged', 'actioned', 'ignored')),
  triage_notes   text,
  deadline       date,
  raw            jsonb not null default '{}'::jsonb,
  created_at     timestamptz not null default now(),
  triaged_at     timestamptz,
  unique (source, external_id)
);

create index if not exists signals_status_idx
  on signals (triage_status, occurred_at desc);
create index if not exists signals_project_idx
  on signals (project_id, occurred_at desc);

create table if not exists connector_state (
  source                text primary key,
  cursor                jsonb not null default '{}'::jsonb,
  last_ok_at            timestamptz,
  last_error            text,
  consecutive_failures  integer not null default 0,
  updated_at            timestamptz not null default now()
);

create table if not exists approval_policy (
  id            uuid primary key default gen_random_uuid(),
  tool_pattern  text not null unique,
  gate          text not null check (gate in ('auto', 'approve', 'deny')),
  note          text,
  created_at    timestamptz not null default now()
);

-- Defaults. Unmatched tools are auto (today every tool is a read or a memory
-- write). Patterns are fnmatch-style over the tool name. Edit rows to tune;
-- never widen the deny list by code.
insert into approval_policy (tool_pattern, gate, note) values
  ('*transfer*',        'deny',    'Money movement is never automated'),
  ('*payment*',         'deny',    'Money movement is never automated'),
  ('*submit*',          'deny',    'Forms and applications are submitted by hand'),
  ('*publish*',         'deny',    'Public posts are never automated'),
  ('*close_account*',   'deny',    'Account changes are never automated'),
  ('*change_password*', 'deny',    'Account changes are never automated'),
  ('*send_*',           'approve', 'Messages leave only after an approval card'),
  ('*reply*',           'approve', 'Replies leave only after an approval card'),
  ('*comment*',         'approve', 'Comments on PRs and issues are approved first'),
  ('*create_event*',    'approve', 'Calendar holds are approved first')
on conflict (tool_pattern) do nothing;

create table if not exists llm_calls (
  id         uuid primary key default gen_random_uuid(),
  called_at  timestamptz not null default now(),
  purpose    text not null,
  model      text not null,
  tier       text,
  usage      jsonb not null default '{}'::jsonb,
  cost_usd   numeric(12, 4) not null default 0
);

create index if not exists llm_calls_called_at_idx on llm_calls (called_at);

-- Deadlines gain provenance back to the project and the signal that
-- created them, so a brief can say where a date came from.
alter table deadlines add column if not exists project_id uuid references projects(id) on delete set null;
alter table deadlines add column if not exists signal_id  uuid references signals(id)  on delete set null;
