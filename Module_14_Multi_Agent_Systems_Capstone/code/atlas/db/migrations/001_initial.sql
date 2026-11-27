-- Atlas Memory Schema (Phase 0)
-- Postgres 16 + pgvector
-- Single-user system, no tenancy
--
-- Migration 001_initial. Generated from docs/atlas-schema.sql with three fixes:
--   1. Idempotency: `if not exists` on every table and index so re-runs are safe.
--   2. updated_at is kept honest via a trigger (currently only entities has updated_at).
--   3. entities.canonical_name has an index for soft-dedup lookups.

create extension if not exists vector;
create extension if not exists pgcrypto;  -- gen_random_uuid()

-- =====================================================
-- Trigger helper: set updated_at = now() on row update.
-- Reused by any table that grows an updated_at column.
-- =====================================================
create or replace function set_updated_at() returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

-- =====================================================
-- 1. SEMANTIC MEMORY
-- Entities, attributes, relationships, embedded facts
-- =====================================================

create table if not exists entities (
  id uuid primary key default gen_random_uuid(),
  type text not null,            -- person, supplier, property, course, account, business, etc.
  name text not null,
  canonical_name text,
  metadata jsonb default '{}',
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create index if not exists idx_entities_type on entities(type);
create index if not exists idx_entities_name on entities(name);
create index if not exists idx_entities_canonical on entities(canonical_name);  -- soft dedup lookups

-- Keep entities.updated_at honest. Drop-then-create is idempotent for triggers.
drop trigger if exists trg_entities_updated_at on entities;
create trigger trg_entities_updated_at
  before update on entities
  for each row execute function set_updated_at();

create table if not exists entity_attributes (
  id uuid primary key default gen_random_uuid(),
  entity_id uuid references entities(id) on delete cascade,
  key text not null,
  value text not null,
  source text,                   -- which agent or session wrote this
  confidence real default 1.0,
  created_at timestamptz default now()
);

create index if not exists idx_attrs_entity on entity_attributes(entity_id);
create index if not exists idx_attrs_key on entity_attributes(key);

create table if not exists relationships (
  id uuid primary key default gen_random_uuid(),
  from_entity uuid references entities(id) on delete cascade,
  to_entity uuid references entities(id) on delete cascade,
  rel_type text not null,        -- owns, supplies, manages, married_to, employs, etc.
  metadata jsonb default '{}',
  created_at timestamptz default now()
);

create index if not exists idx_rel_from on relationships(from_entity);
create index if not exists idx_rel_to on relationships(to_entity);
create index if not exists idx_rel_type on relationships(rel_type);

-- Unstructured semantic: embedded fact statements for fuzzy retrieval
create table if not exists facts (
  id uuid primary key default gen_random_uuid(),
  content text not null,
  embedding vector(768),         -- nomic-embed-text dimensions
  entity_ids uuid[] default '{}',
  source text,
  confidence real default 1.0,
  created_at timestamptz default now(),
  last_accessed timestamptz default now()
);

create index if not exists idx_facts_embedding on facts using hnsw (embedding vector_cosine_ops);
create index if not exists idx_facts_entities on facts using gin (entity_ids);

-- =====================================================
-- 2. EPISODIC MEMORY
-- Session narratives
-- =====================================================

create table if not exists episodes (
  id uuid primary key default gen_random_uuid(),
  started_at timestamptz not null,
  ended_at timestamptz,
  agent_name text,
  participants text[],           -- "owner", "ceo", "sourcing", "external:client_name"
  summary text not null,
  outcome text,
  what_changed jsonb default '{}',
  transcript_path text,          -- pointer to full transcript file if kept
  embedding vector(768),
  tags text[] default '{}'
);

create index if not exists idx_episodes_started on episodes(started_at desc);
create index if not exists idx_episodes_agent on episodes(agent_name);
create index if not exists idx_episodes_embedding on episodes using hnsw (embedding vector_cosine_ops);
create index if not exists idx_episodes_tags on episodes using gin (tags);

-- =====================================================
-- 3. PROCEDURAL MEMORY
-- Index into git-backed markdown procedure files
-- =====================================================

create table if not exists procedures (
  id uuid primary key default gen_random_uuid(),
  path text not null unique,     -- "sourcing/alibaba_supplier_eval.md"
  domain text not null,          -- sourcing, property_ops, academic, etc.
  name text not null,
  version int default 1,
  description text,
  content_hash text,             -- detect when file changed on disk
  embedding vector(768),
  last_loaded_at timestamptz default now(),
  created_at timestamptz default now()
);

create index if not exists idx_procedures_domain on procedures(domain);
create index if not exists idx_procedures_embedding on procedures using hnsw (embedding vector_cosine_ops);

-- =====================================================
-- 4. PROSPECTIVE MEMORY
-- Time and event triggers
-- =====================================================

create table if not exists triggers (
  id uuid primary key default gen_random_uuid(),
  trigger_type text not null,    -- time, event, condition
  spec jsonb not null,           -- cron expression, event filter, or condition predicate
  payload jsonb not null,        -- agent + task to fire
  status text default 'active',  -- active, fired, cancelled
  description text,
  created_at timestamptz default now(),
  fires_at timestamptz,          -- next fire time for time-based
  fired_at timestamptz,
  fire_count int default 0
);

create index if not exists idx_triggers_status on triggers(status);
create index if not exists idx_triggers_fires_at on triggers(fires_at) where status = 'active';

-- =====================================================
-- 5. GOALS
-- What the CEO is tracking
-- =====================================================

create table if not exists goals (
  id uuid primary key default gen_random_uuid(),
  description text not null,
  owner text default 'ceo',
  status text default 'active',  -- active, paused, closed, abandoned
  success_criteria text,
  created_at timestamptz default now(),
  target_date date,
  closed_at timestamptz,
  metadata jsonb default '{}'
);

create index if not exists idx_goals_status on goals(status);

-- Link goals to episodes that progressed them
create table if not exists goal_episodes (
  goal_id uuid references goals(id) on delete cascade,
  episode_id uuid references episodes(id) on delete cascade,
  contribution text,             -- how this episode advanced the goal
  primary key (goal_id, episode_id)
);

-- =====================================================
-- 6. AUDIT LOG
-- Every agent action for "why did you do X"
-- =====================================================

create table if not exists actions (
  id uuid primary key default gen_random_uuid(),
  agent_name text not null,
  action_type text not null,     -- memory_write, tool_call, message_sent, dispatch, approval_request
  inputs jsonb,
  outputs jsonb,
  approved_by text,              -- "auto" or "user"
  episode_id uuid references episodes(id) on delete set null,
  goal_id uuid references goals(id) on delete set null,
  created_at timestamptz default now()
);

create index if not exists idx_actions_agent on actions(agent_name);
create index if not exists idx_actions_type on actions(action_type);
create index if not exists idx_actions_episode on actions(episode_id);
create index if not exists idx_actions_created on actions(created_at desc);

-- =====================================================
-- 7. APPROVAL QUEUE
-- Pending actions awaiting user approval via Telegram
-- =====================================================

create table if not exists approvals (
  id uuid primary key default gen_random_uuid(),
  agent_name text not null,
  action_summary text not null,  -- human-readable description
  action_payload jsonb not null, -- the actual action to execute on approval
  status text default 'pending', -- pending, approved, rejected, expired
  telegram_message_id bigint,    -- for updating the inline keyboard
  created_at timestamptz default now(),
  resolved_at timestamptz,
  expires_at timestamptz
);

create index if not exists idx_approvals_status on approvals(status);
