-- 019: the answers Trilok gives once so Atlas can fill job applications.
--
-- candidate_answers  one row per question. Standard questions (contact,
--                    work authorization, sponsorship, start date, the
--                    voluntary EEO set) are defined in core/jobs/answers.py
--                    and get a row when first answered. Any other question
--                    an application form asks is recorded here unanswered
--                    the first time Atlas meets it; that application waits
--                    until Trilok answers, and the answer is reused after.
--
-- The answers are personal (phone, email, authorization status), which is
-- why they live in the database and not in config/jobs.yaml, which is
-- committed. Atlas never logs an answer.

create table if not exists candidate_answers (
  key            text primary key,
  question       text not null,
  answer         text,
  standard       boolean not null default false,
  asked_count    integer not null default 0,
  first_asked_at timestamptz,
  last_asked_at  timestamptz,
  answered_at    timestamptz,
  context        jsonb not null default '{}'::jsonb,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);

create index if not exists candidate_answers_pending_idx
  on candidate_answers (last_asked_at desc) where answer is null;
