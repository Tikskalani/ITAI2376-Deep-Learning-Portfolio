-- 025: the night shift (docs/atlas-vision-2026-10-10.md, section 4).
--
-- night_tasks  one row per task Atlas works while Trilok sleeps. Every row
--              carries the task contract: the project, a definition of
--              done, the agent that does it, the engine, a budget. Nothing
--              runs without his approval; nothing it does leaves the house
--              (the agents prepare, the usual approval gates still apply).
--
--   proposed   Atlas suggested it (from a project's next action) or he typed it
--   approved   he tapped Approve (tasks he adds himself start here)
--   skipped    he tapped Skip
--   running    the agent is on it
--   done       the agent reported DONE: with where the result is
--   needs_you  the agent stopped: it needs a decision, a fact, or his hands
--   failed     the run errored
--   held       not started: the nightly or daily cap was reached first
--
-- review is his morning verdict on a done or needs_you task: accepted or
-- rework. That verdict is the Overnight-yield measure of the Atlas Standard.

create table if not exists night_tasks (
  id                 uuid primary key default gen_random_uuid(),
  night              date not null,
  project_id         uuid references projects(id) on delete set null,
  title              text not null,
  definition_of_done text not null,
  agent              text not null,
  engine             text not null default 'api' check (engine in ('api', 'claude')),
  budget_usd         numeric(8, 4),
  status             text not null default 'proposed'
                     check (status in ('proposed', 'approved', 'skipped', 'running', 'done', 'needs_you',
                                       'failed', 'held')),
  proposed_by        text not null default 'atlas',
  episode_id         uuid references episodes(id) on delete set null,
  result             text,
  error              text,
  cost_usd           numeric(10, 4) not null default 0,
  review             text check (review in ('accepted', 'rework')),
  review_note        text,
  reviewed_at        timestamptz,
  reported_at        timestamptz,
  created_at         timestamptz not null default now(),
  started_at         timestamptz,
  finished_at        timestamptz,
  updated_at         timestamptz not null default now()
);

create index if not exists night_tasks_night_idx on night_tasks (night desc, status);
