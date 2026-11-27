-- Atlas Memory Schema -- Migration 014_memory_v2
-- R5: hybrid retrieval, provenance, context packs, the always-loaded profile.
--
--   tsv columns     generated tsvectors with GIN indexes on facts, episodes,
--                   lessons and signals, so retrieval can fuse lexical and
--                   vector results (a name or a number that embeddings miss)
--   provenance      facts, episodes and lessons gain project_id and the
--                   signal or episode they came from
--   context packs   projects.context_pack, regenerated nightly by the
--                   consolidation job, served to Claude over MCP
--   profile_notes   the few lines every run always sees: standing facts,
--                   decisions, and constraints that expire
--
-- Idempotent.

alter table facts    add column if not exists project_id uuid references projects(id) on delete set null;
alter table facts    add column if not exists signal_id  uuid references signals(id)  on delete set null;
alter table facts    add column if not exists episode_id uuid references episodes(id) on delete set null;
alter table episodes add column if not exists project_id uuid references projects(id) on delete set null;
alter table lessons  add column if not exists project_id uuid references projects(id) on delete set null;

alter table facts add column if not exists tsv tsvector
  generated always as (to_tsvector('english', coalesce(content, ''))) stored;
create index if not exists facts_tsv_idx on facts using gin (tsv);

alter table episodes add column if not exists tsv tsvector
  generated always as (to_tsvector('english', coalesce(summary, '') || ' ' || coalesce(outcome, ''))) stored;
create index if not exists episodes_tsv_idx on episodes using gin (tsv);

alter table lessons add column if not exists tsv tsvector
  generated always as (to_tsvector('english', coalesce(content, ''))) stored;
create index if not exists lessons_tsv_idx on lessons using gin (tsv);

alter table signals add column if not exists tsv tsvector
  generated always as (to_tsvector('english', coalesce(subject, '') || ' ' || coalesce(body, ''))) stored;
create index if not exists signals_tsv_idx on signals using gin (tsv);

alter table projects add column if not exists context_pack    text;
alter table projects add column if not exists context_pack_at timestamptz;

create table if not exists profile_notes (
  id          uuid primary key default gen_random_uuid(),
  kind        text not null default 'fact'
              check (kind in ('fact', 'decision', 'constraint', 'preference')),
  content     text not null,
  source      text not null default 'owner',
  expires_at  timestamptz,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create index if not exists profile_notes_expires_idx on profile_notes (expires_at, created_at);
