-- 030: outcome learning.
--
-- episode_feedback already holds Trilok's good / bad on a run. It now says
-- what kind of signal it was: a tap on the trace, the morning review of a
-- night task, a company's reply to an application, or a correction in chat.
--
-- eval_candidates: every bad outcome proposes an eval case from the run
-- that caused it (the task, what came out, what he said), so a mistake can
-- be turned into a test instead of happening twice. A candidate is written
-- into evals/data by scripts/eval_candidates.py, or dismissed.

alter table episode_feedback add column if not exists kind text not null default 'tap';

create table if not exists eval_candidates (
  id          uuid primary key default gen_random_uuid(),
  episode_id  uuid references episodes(id) on delete set null,
  agent       text,
  task        text,
  result      text,
  note        text,
  verdict     text not null check (verdict in ('good', 'bad')),
  kind        text not null,
  source      text not null default 'owner',
  status      text not null default 'proposed' check (status in ('proposed', 'written', 'dismissed')),
  suite       text,
  created_at  timestamptz not null default now(),
  decided_at  timestamptz
);

create index if not exists eval_candidates_status_idx on eval_candidates (status, created_at desc);
