-- 034: reliability. The nightly self-check's verdicts, and the setup chores
-- off the Plan.
--
-- slo_checks holds one row per day per objective: brief/morning and
-- brief/evening (delivered within 15 minutes of the slot) and heartbeat (the
-- executive loop never went longer than its stale limit without a tick).
-- core/reliability.py writes it at 23:50 and /api/health and the Bridge read
-- the last seven days. No outward tool is added, so no approval_policy row.
--
-- The setup chores (connect Google, Canvas and GitHub; install the submitter
-- browser) now live on one checklist in the Links room with a live status
-- each. The open copies the CEO put on the Plan on 2026-10-11 are cancelled
-- here; agents can no longer add them (agents/ceo/tools.py). Owner-made
-- tasks are left alone. Idempotent: a second run finds nothing open.

create table if not exists slo_checks (
    day        date        not null,
    objective  text        not null,
    ok         boolean     not null,
    value      numeric,
    target     numeric,
    detail     text,
    checked_at timestamptz not null default now(),
    primary key (day, objective)
);

create index if not exists slo_checks_day_idx on slo_checks (day desc);

with chores as (
    update deadlines
       set status = 'cancelled', completed_at = now()
     where source = 'ceo'
       and status = 'open'
       and (title ilike '%connect.bat%' or title ilike '%playwright install%' or title ilike '%submitter browser%')
    returning trigger_id
)
update triggers set status = 'cancelled'
 where id in (select trigger_id from chores where trigger_id is not null)
   and status = 'active';
