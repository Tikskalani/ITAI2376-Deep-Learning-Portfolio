-- 038: capabilities (K2C, Trilok's spec of 2026-10-20): a video, article or
-- page becomes a source-checked, versioned procedure Atlas agents can
-- retrieve. core/capabilities/, docs/capabilities.md.
--
-- The simplest schema that keeps the distinctions the spec asks for:
--   cap_sources      what was shared, its processing status and spend
--   cap_attempts     each processing run of a source (dedupe, refresh, retries)
--   cap_evidence     timed transcript segments, frames, screen text,
--                    descriptions and resolved resources, each labelled
--                    exact / approximate / unreadable
--   cap_claims       actionable claims with the evidence they cite and how
--                    faithfully the source carries them (extraction fidelity)
--   cap_checks       external checks of a claim: verdict, evidence, date
--   cap_packages     a procedure's identity (and its project scope)
--   cap_versions     immutable procedure content; a trigger refuses edits
--   cap_reviews      approval and withdrawal, separate from the content
--   cap_uses         a version retrieved in a run (exact version, run, agent)
--   cap_outcomes     what happened, bound to a use
--   cap_exports      a version installed as a Claude Code skill, and removed
-- Four dimensions never collapse into one flag: extraction fidelity (claims),
-- external support (checks), practical test status (versions) and human
-- approval (reviews). Idempotent.

create table if not exists cap_sources (
  id            uuid primary key default gen_random_uuid(),
  url           text not null,
  canonical     text not null unique,          -- youtube:<id>, or the normalized url
  kind          text not null default 'video' check (kind in ('video', 'page')),
  title         text,
  author        text,
  status        text not null default 'queued'
                check (status in ('queued', 'processing', 'held_budget', 'failed', 'done')),
  stage         text,                          -- the stage running, held or failed
  error         text,
  cost_usd      numeric(10, 4) not null default 0,
  cap_usd       numeric(10, 4) not null,
  attempts      int not null default 0,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

create table if not exists cap_attempts (
  id            uuid primary key default gen_random_uuid(),
  source_id     uuid not null references cap_sources(id) on delete cascade,
  reason        text not null check (reason in ('first', 'retry', 'refresh')),
  status        text not null default 'running'
                check (status in ('running', 'held_budget', 'failed', 'done')),
  stage         text,
  error         text,
  cost_usd      numeric(10, 4) not null default 0,
  started_at    timestamptz not null default now(),
  ended_at      timestamptz
);
create index if not exists cap_attempts_source_idx on cap_attempts (source_id, started_at desc);

create table if not exists cap_evidence (
  id            uuid primary key default gen_random_uuid(),
  source_id     uuid not null references cap_sources(id) on delete cascade,
  attempt_id    uuid references cap_attempts(id) on delete set null,
  ref           text not null,                 -- E1, E2... as the claims and steps cite them
  kind          text not null check (kind in ('transcript', 'frame', 'screen_text', 'description', 'resource')),
  start_s       numeric(9, 2),
  end_s         numeric(9, 2),
  text          text,
  file          text,                          -- under data/capabilities/<source>/, relative
  url           text,                          -- a resource's original URL
  extraction    text not null default 'exact'
                check (extraction in ('exact', 'approximate', 'unreadable', 'unavailable')),
  meta          jsonb not null default '{}',
  created_at    timestamptz not null default now(),
  unique (attempt_id, ref)
);
create index if not exists cap_evidence_source_idx on cap_evidence (source_id, kind);

create table if not exists cap_claims (
  id            uuid primary key default gen_random_uuid(),
  source_id     uuid not null references cap_sources(id) on delete cascade,
  attempt_id    uuid references cap_attempts(id) on delete set null,
  text          text not null,
  quote         text,
  evidence_refs text[] not null default '{}',
  fidelity      text not null
                check (fidelity in ('quoted', 'paraphrased', 'from_visual', 'from_resource', 'not_located')),
  consequential boolean not null default false,
  created_at    timestamptz not null default now()
);

create table if not exists cap_checks (
  id            uuid primary key default gen_random_uuid(),
  claim_id      uuid not null references cap_claims(id) on delete cascade,
  verdict       text not null check (verdict in ('supported', 'contradicted', 'inconclusive', 'not_checked')),
  checked_url   text,
  excerpt       text,
  method        text not null,
  checked_at    timestamptz not null default now()
);

create table if not exists cap_packages (
  id            uuid primary key default gen_random_uuid(),
  slug          text not null unique,
  title         text not null,
  project_slug  text,                          -- null: reusable public material; else only that project
  created_at    timestamptz not null default now()
);

create table if not exists cap_versions (
  id            uuid primary key default gen_random_uuid(),
  package_id    uuid not null references cap_packages(id) on delete cascade,
  version       int not null,
  source_id     uuid references cap_sources(id) on delete set null,
  attempt_id    uuid references cap_attempts(id) on delete set null,
  body          jsonb not null,                -- summary, prerequisites, steps (with evidence), limitations, missing
  check_status  text not null check (check_status in ('partially_checked', 'not_checked')),
  test_status   text not null default 'not_practically_tested'
                check (test_status in ('not_practically_tested', 'human_validated')),
  complete      boolean not null default true, -- false: a resource the steps need could not be read
  created_at    timestamptz not null default now(),
  unique (package_id, version)
);

-- A version's content never changes: approval and withdrawal are records of
-- their own (cap_reviews), so history stays as it was approved.
create or replace function cap_versions_immutable() returns trigger as $$
begin
  raise exception 'cap_versions are immutable; record a review or write a new version';
end $$ language plpgsql;
drop trigger if exists cap_versions_immutable on cap_versions;
create trigger cap_versions_immutable before update on cap_versions
  for each row execute function cap_versions_immutable();

create table if not exists cap_reviews (
  id            uuid primary key default gen_random_uuid(),
  version_id    uuid not null references cap_versions(id) on delete cascade,
  decision      text not null check (decision in ('approved', 'withdrawn')),
  reviewer      text not null default 'trilok',
  note          text,
  created_at    timestamptz not null default now()
);
create index if not exists cap_reviews_version_idx on cap_reviews (version_id, created_at desc);

create table if not exists cap_uses (
  id            uuid primary key default gen_random_uuid(),
  version_id    uuid not null references cap_versions(id) on delete cascade,
  episode_id    uuid,
  agent_name    text not null,
  project_slug  text,
  created_at    timestamptz not null default now()
);
create index if not exists cap_uses_episode_idx on cap_uses (episode_id, version_id);

create table if not exists cap_outcomes (
  id            uuid primary key default gen_random_uuid(),
  use_id        uuid not null references cap_uses(id) on delete cascade,
  verdict       text not null check (verdict in ('worked', 'partly', 'did_not_work', 'not_applicable')),
  note          text,
  reported_by   text not null,
  created_at    timestamptz not null default now()
);

create table if not exists cap_exports (
  id            uuid primary key default gen_random_uuid(),
  version_id    uuid not null references cap_versions(id) on delete cascade,
  path          text not null,
  created_at    timestamptz not null default now(),
  removed_at    timestamptz
);

-- The CTO's three tools: two reads and one write bound to the run.
insert into approval_policy (tool_pattern, gate, note) values
  ('search_capabilities',       'auto', 'Read: approved, not withdrawn procedures in the run''s scope'),
  ('get_capability',            'auto', 'Read: one approved version as untrusted reference; records the use'),
  ('report_capability_outcome', 'auto', 'Atlas write: an outcome for a version this run retrieved; refused otherwise')
on conflict (tool_pattern) do nothing;
