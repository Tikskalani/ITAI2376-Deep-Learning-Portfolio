-- Atlas Memory Schema -- Migration 015_skills
-- R5b: the teach loop. A reel or video Trilok adopts no longer stops at a
-- procedure file and a lesson that agents only see if they search. It
-- becomes a skill: a short playbook that is injected into every run of
-- the agents it applies to, counted each time it is used, rated from the
-- outcomes, and reviewed on Sunday so the ones that do not earn their
-- place get retired.
--
--   skills            the playbook lines, which agents they apply to, status
--   skill_uses        which episode ran with which skill in its prompt
--   episode_feedback  Trilok's good / bad on an episode (from the PWA or MCP)
--   eval_runs         persisted eval results, so pass rates can be compared
--                     before and after a skill was adopted
--
-- Idempotent.

create table if not exists skills (
  id                   uuid primary key default gen_random_uuid(),
  slug                 text not null unique,
  title                text not null,
  body                 text not null,
  domain               text,
  applies_to           text[] not null default '{all}',
  status               text not null default 'trial'
                       check (status in ('trial', 'active', 'retired')),
  source_candidate_id  uuid references learning_candidates(id) on delete set null,
  source_url           text,
  procedure_path       text,
  uses                 integer not null default 0,
  good                 integer not null default 0,
  bad                  integer not null default 0,
  last_used_at         timestamptz,
  retired_at           timestamptz,
  retire_reason        text,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now()
);

create index if not exists skills_status_idx on skills (status, created_at desc);

create table if not exists skill_uses (
  skill_id    uuid not null references skills(id) on delete cascade,
  episode_id  uuid not null references episodes(id) on delete cascade,
  created_at  timestamptz not null default now(),
  primary key (skill_id, episode_id)
);

create index if not exists skill_uses_episode_idx on skill_uses (episode_id);

create table if not exists episode_feedback (
  episode_id  uuid primary key references episodes(id) on delete cascade,
  verdict     text not null check (verdict in ('good', 'bad')),
  note        text,
  source      text not null default 'owner',
  created_at  timestamptz not null default now()
);

create table if not exists eval_runs (
  id          uuid primary key default gen_random_uuid(),
  suite       text not null,
  "case"      text not null,
  passed      boolean not null,
  details     text[] not null default '{}',
  created_at  timestamptz not null default now()
);

create index if not exists eval_runs_suite_idx on eval_runs (suite, created_at desc);
