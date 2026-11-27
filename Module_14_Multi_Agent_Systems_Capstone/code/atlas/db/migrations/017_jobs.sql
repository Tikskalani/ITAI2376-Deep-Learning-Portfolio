-- 017: the job search engine (R9b).
--
-- job_postings      roles found on company job boards (Greenhouse, Lever,
--                   Ashby), filtered in code and scored by Haiku
-- job_applications  every application Trilok made, one row per
--                   company and role, its status and next action
-- job_events        what moved an application: a confirmation, a reply,
--                   an interview invite, a rejection, a note. A Gmail
--                   message id is recorded once, so a re-scan is a no-op.

create table if not exists job_postings (
  id            uuid primary key default gen_random_uuid(),
  source        text not null check (source in ('greenhouse', 'lever', 'ashby', 'manual')),
  board         text not null,
  external_id   text not null,
  company       text not null,
  title         text not null,
  location      text,
  remote        boolean,
  url           text,
  salary_min    integer,
  salary_max    integer,
  posted_at     timestamptz,
  description   text,
  score         smallint check (score between 0 and 100),
  reason        text,
  status        text not null default 'new'
                check (status in ('new', 'shown', 'dismissed', 'applied', 'closed')),
  first_seen_at timestamptz not null default now(),
  last_seen_at  timestamptz not null default now(),
  unique (source, board, external_id)
);

create index if not exists job_postings_score_idx on job_postings (status, score desc nulls last, first_seen_at desc);

create table if not exists job_applications (
  id              uuid primary key default gen_random_uuid(),
  key             text not null unique,
  company         text not null,
  role            text not null,
  url             text,
  source          text not null default 'manual',
  status          text not null default 'applied'
                  check (status in ('interested', 'applied', 'screening', 'interview', 'offer',
                                    'rejected', 'withdrawn', 'ghosted')),
  applied_at      timestamptz,
  last_contact_at timestamptz,
  next_action     text,
  next_action_at  timestamptz,
  contact         text,
  notes           text,
  posting_id      uuid references job_postings(id) on delete set null,
  thread_ids      text[] not null default '{}',
  materials       jsonb not null default '{}'::jsonb,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

create index if not exists job_applications_status_idx on job_applications (status, next_action_at);

create table if not exists job_events (
  id             uuid primary key default gen_random_uuid(),
  application_id uuid not null references job_applications(id) on delete cascade,
  at             timestamptz not null default now(),
  kind           text not null,
  detail         text,
  message_id     text unique
);

create index if not exists job_events_app_idx on job_events (application_id, at desc);
