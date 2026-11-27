-- Atlas Migration 004 — COO operations: deadlines and task templates
-- Postgres 16 + pgvector. Single-user, no tenancy.
--
-- Two tables behind the COO agent:
--   deadlines      — dated commitments the COO tracks to completion. A
--                    deadline can carry a prospective trigger that fires
--                    a reminder before it is due, so it flows through the
--                    executive loop without any change to the loop.
--   task_templates — recurring work the COO knows to expect: monthly
--                    statement reconciliation, quarterly tax estimates,
--                    annual ACA renewal. The COO turns due templates into
--                    concrete deadlines.
--
-- Idempotent: `if not exists` on every object so re-runs are safe.

-- =====================================================
-- DEADLINES
-- =====================================================

create table if not exists deadlines (
  id uuid primary key default gen_random_uuid(),
  title text not null,
  due_date date not null,
  domain text,                      -- side_shop, property, hcc, money, etc.
  priority text not null default 'normal',  -- low, normal, high
  status text not null default 'open',      -- open, done, missed, cancelled
  notes text,
  source text not null default 'owner',     -- owner, or an agent name
  trigger_id uuid references triggers(id) on delete set null,
  created_at timestamptz default now(),
  completed_at timestamptz,
  metadata jsonb default '{}'
);

create index if not exists idx_deadlines_status on deadlines(status);
create index if not exists idx_deadlines_due_date on deadlines(due_date);
create index if not exists idx_deadlines_domain on deadlines(domain);

-- =====================================================
-- TASK TEMPLATES
-- =====================================================

create table if not exists task_templates (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  cadence text not null,            -- weekly, monthly, quarterly, annual
  domain text,
  lead_time_days int not null default 0,  -- start this many days before due
  checklist text,                   -- what the task involves
  status text not null default 'active',  -- active, paused
  last_run date,                    -- last date this template was instantiated
  created_at timestamptz default now(),
  metadata jsonb default '{}'
);

create index if not exists idx_task_templates_status on task_templates(status);
