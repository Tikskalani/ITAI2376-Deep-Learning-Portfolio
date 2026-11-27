-- 020: the daily apply queue (auto-apply PR C).
--
-- job_queue  one row per posting Atlas picked for Trilok's morning queue.
--            A posting is queued at most once, ever. Atlas drafts the
--            tailored resume and cover note before he sees it; his tap
--            (approved or skipped) is recorded here. The submitter (PR D)
--            moves approved rows on to submitted.
--
--   drafting         picked; the career agent is writing the materials
--   ready            materials drafted, waiting for Trilok
--   needs_attention  drafting failed or a check tripped; `error` says why
--   approved         Trilok tapped Apply
--   skipped          Trilok tapped Skip
--   closed           the posting left its board before he decided
--   submitted        the submitter sent it (PR D)

create table if not exists job_queue (
  id             uuid primary key default gen_random_uuid(),
  posting_id     uuid not null unique references job_postings(id) on delete cascade,
  application_id uuid references job_applications(id) on delete set null,
  queued_for     date not null default current_date,
  status         text not null default 'drafting'
                 check (status in ('drafting', 'ready', 'needs_attention', 'approved', 'skipped',
                                   'closed', 'submitted')),
  flags          text[] not null default '{}',
  error          text,
  decided_at     timestamptz,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);

create index if not exists job_queue_open_idx
  on job_queue (queued_for desc, created_at) where status in ('drafting', 'ready', 'needs_attention', 'approved');
