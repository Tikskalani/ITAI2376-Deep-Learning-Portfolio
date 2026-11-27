-- 023: the job submitter (auto-apply PR D).
--
-- job_submissions  one row per posting Atlas tries to submit, ever. The
--                  posting is the idempotency key: a retry, a second tap or
--                  a second worker finds this row and never submits twice.
--                  Evidence (the exact PDF, cover note and answers, their
--                  hashes, the screenshots) is written per attempt and never
--                  overwritten.
--
--   checking      claimed; the pre-submit check is running
--   needs_answer  a required form question has no saved answer; asked once
--   needs_click   Trilok must finish it by hand: a CAPTCHA or login wall, no
--                 adapter for this board yet, an adapter that keeps failing,
--                 or a submit whose outcome was not seen
--   blocked       the pre-submit check failed; `reason` says why
--   dry_run       filled and screenshotted, not sent (the first week)
--   submitting    the submit button was pressed; if a crash leaves a row
--                 here it is never retried, because it may have gone through
--   submitted     the confirmation page was seen
--   confirmed     the acknowledgement email was matched (PR F)

create table if not exists job_submissions (
  id              uuid primary key default gen_random_uuid(),
  posting_id      uuid not null unique references job_postings(id) on delete cascade,
  queue_id        uuid references job_queue(id) on delete set null,
  application_id  uuid references job_applications(id) on delete set null,
  ats             text not null,
  status          text not null default 'checking'
                  check (status in ('checking', 'needs_answer', 'needs_click', 'blocked', 'dry_run',
                                    'submitting', 'submitted', 'confirmed')),
  dry_run         boolean not null default true,
  attempts        integer not null default 0,
  reason          text,
  -- How the board adapter did on the last real (not dry-run) attempt: true
  -- when the confirmation page was seen, false when it broke or saw none,
  -- null when it never got that far. Feeds the per-board success rate.
  adapter_ok      boolean,
  evidence        jsonb not null default '[]'::jsonb,
  submitted_at    timestamptz,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

create index if not exists job_submissions_ats_idx on job_submissions (ats, updated_at desc);
