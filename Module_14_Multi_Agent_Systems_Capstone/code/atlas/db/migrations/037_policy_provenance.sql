-- 037: every tool has an explicit policy; skills and procedures say where
-- they came from.
--
-- 1. approval_policy gets one row per tool Atlas ships, so "allowed on
--    purpose" and "nobody classified it" are different things. The runtime
--    now refuses a tool no row matches (core/runtime/policy.py), and
--    tests/test_tool_policy_coverage.py fails when a registered tool has no
--    row here. Gates follow what each handler does (the review is in the
--    PR), not its name. Reads and writes that stay inside Atlas keep running
--    on their own, as before. Four change:
--      dispatch_agent, run_workflow, queue_for_tonight: delegated work does
--        not yet inherit the caller's scope or have bounded depth, fan-out,
--        retries or spend, so it asks first until it does.
--      draft_email: creates an object in Trilok's Gmail.
--      import_statement: reads a local file at any path the model names.
--    server:web_search / server:web_fetch decide whether an agent is given
--    the Anthropic-hosted tools at all (Atlas never sees the calls).
--    "on conflict do nothing": a gate Trilok has changed stays changed.
--
-- 2. skills.provenance: user_authored, external_internet, agent_reflection
--    or unknown. Only user_authored skills, and agent_reflection skills
--    Trilok has approved (skill_reviews), reach a system prompt. Approval is
--    a separate record and never changes provenance; nothing internet-derived
--    or of unknown origin is injected, approved or not. Backfilled from the
--    learning candidate each skill came from; no row becomes user_authored.
--
-- 3. procedure_provenance: the same labels for procedure files, which
--    agents read as tool results. A file with no row is unknown.
--
-- Idempotent.

-- 1 ---------------------------------------------------------------------

insert into approval_policy (tool_pattern, gate, note) values
  -- reads
  ('cash_flow',             'auto', 'Read: transactions'),
  ('check_slipping',        'auto', 'Read: deadlines'),
  ('detect_anomalies',      'auto', 'Read: transactions'),
  ('explain_run',           'auto', 'Read: a past run, including its tool inputs and results'),
  ('get_application',       'auto', 'Read: one job application'),
  ('get_posting',           'auto', 'Read: one job posting, cleaned of AI-aimed lines'),
  ('household_deadlines',   'auto', 'Read: deadlines'),
  ('list_applications',     'auto', 'Read: job applications'),
  ('list_deadlines',        'auto', 'Read: deadlines'),
  ('list_goals',            'auto', 'Read: goals'),
  ('list_household_bills',  'auto', 'Read: recurring bills'),
  ('list_postings',         'auto', 'Read: job postings'),
  ('list_recurring_bills',  'auto', 'Read: recurring bills'),
  ('list_task_templates',   'auto', 'Read: task templates'),
  ('list_transactions',     'auto', 'Read: transactions'),
  ('plan_week',             'auto', 'Read: the week across deadlines, signals and night tasks'),
  ('portfolio',             'auto', 'Read: trades'),
  ('read_resume',           'auto', 'Read: the newest resume file'),
  ('recall_facts',          'auto', 'Read: semantic memory (marks facts accessed)'),
  ('recall_lessons',        'auto', 'Read: lessons'),
  ('upcoming_events',       'auto', 'Read: the calendar'),
  ('load_procedure',        'auto', 'Read: a .md file inside the procedure roots'),
  ('fetch_url',             'auto', 'Read: a public web page; private and local addresses refused'),
  ('fetch_transcript',      'auto', 'Read: a public video''s transcript; site extractors only. Can use browser cookies when configured'),
  -- writes that stay inside Atlas
  ('add_application',       'auto', 'Atlas write: job applications'),
  ('add_deadline',          'auto', 'Atlas write: one deadline and its reminder'),
  ('add_household_deadline','auto', 'Atlas write: one deadline and its reminder'),
  ('add_recurring_bill',    'auto', 'Atlas write: recurring bills'),
  ('add_task',              'auto', 'Atlas write: a task; a recurring rule writes many. Flagged for review'),
  ('add_task_template',     'auto', 'Atlas write: task templates'),
  ('categorize',            'auto', 'Atlas write: transaction categories, many rows at once, overwrites when asked. Flagged for review'),
  ('close_goal',            'auto', 'Atlas write: goal status'),
  ('complete_deadline',     'auto', 'Atlas write: deadline status'),
  ('complete_task',         'auto', 'Atlas write: task status (fuzzy title match)'),
  ('create_goal',           'auto', 'Atlas write: goals'),
  ('dismiss_posting',       'auto', 'Atlas write: posting status'),
  ('log_decision',          'auto', 'Atlas write: decisions, recorded as made by the owner. Flagged for review'),
  ('propose_learning',      'auto', 'Atlas write: a learning candidate for review; adopts nothing'),
  ('rate_run',              'auto', 'Atlas write: a run verdict, overwrites the last one and is recorded as the owner''s. Flagged for review'),
  ('record_lesson',         'auto', 'Atlas write: lessons later runs recall. Flagged for review (memory from untrusted input)'),
  ('remember_fact',         'auto', 'Atlas write: semantic memory. Flagged for review (memory from untrusted input)'),
  ('resolve_decision',      'auto', 'Atlas write: decision outcome'),
  ('save_materials',        'auto', 'Atlas write: application materials, replaces the last draft of a kind. Flagged for review'),
  ('update_application',    'auto', 'Atlas write: application status'),
  ('update_recurring_bill', 'auto', 'Atlas write: overwrites a bill. Flagged for review'),
  ('update_task_template',  'auto', 'Atlas write: overwrites a template. Flagged for review'),
  ('optimize_resume',       'auto', 'Atlas write: replaces the tailored resume draft, asks questions in Jobs, sends a notice. Paid model calls, capped per run and per day. Flagged for review'),
  ('revise_with_answers',   'auto', 'Atlas write: replaces the tailored resume draft from answers typed in Jobs. Paid model calls, capped per run and per day. Flagged for review'),
  -- asks first
  ('calendar_create_event', 'approve', 'Writes to Google Calendar, may invite attendees'),
  ('draft_email',           'approve', 'Creates a draft in Trilok''s Gmail; drafting text inside Atlas needs no tool'),
  ('import_statement',      'approve', 'Reads a local file at any path the model names, then writes many transactions'),
  ('dispatch_agent',        'approve', 'Delegation: the child gets its own full toolset, no carried project scope, no depth, fan-out or spend bound'),
  ('run_workflow',          'approve', 'Delegation: runs agent steps with retries; same gaps as dispatch_agent'),
  ('queue_for_tonight',     'approve', 'Delegation: queues an agent run for the night shift, which runs it without asking again'),
  -- Anthropic-hosted tools: whether the agent is given them at all
  ('server:web_search',     'auto', 'Attach web search (Anthropic runs each call; Atlas cannot gate them one by one)'),
  ('server:web_fetch',      'auto', 'Attach web fetch (Anthropic runs each call; public internet only)')
on conflict (tool_pattern) do nothing;

-- 2 ---------------------------------------------------------------------

alter table skills add column if not exists provenance text not null default 'unknown';
alter table skills add column if not exists source_ref text;

do $$ begin
  alter table skills add constraint skills_provenance_check
    check (provenance in ('user_authored', 'external_internet', 'agent_reflection', 'unknown'));
exception when duplicate_object then null; end $$;

-- Backfill from the candidate each skill was adopted from. Only rows still
-- unknown are touched, and nothing is ever set to user_authored here.
update skills s
   set provenance = case c.source_type
                      when 'reflection' then 'agent_reflection'
                      when 'youtube'    then 'external_internet'
                      when 'instagram'  then 'external_internet'
                      else 'unknown'
                    end,
       source_ref = coalesce(s.source_ref, s.source_url, c.source_url)
  from learning_candidates c
 where c.id = s.source_candidate_id and s.provenance = 'unknown';

-- Provenance is set once. It may move from unknown to a known outside
-- origin; nothing may become user_authored after the fact, and a known
-- origin never changes. Approval lives in skill_reviews instead.
create or replace function skills_provenance_fixed() returns trigger as $$
begin
  if new.provenance is distinct from old.provenance then
    if old.provenance <> 'unknown' or new.provenance = 'user_authored' then
      raise exception 'skills.provenance is fixed (% -> %); record a review instead',
        old.provenance, new.provenance;
    end if;
  end if;
  return new;
end $$ language plpgsql;

drop trigger if exists skills_provenance_fixed on skills;
create trigger skills_provenance_fixed before update on skills
  for each row execute function skills_provenance_fixed();

create table if not exists skill_reviews (
  id          uuid primary key default gen_random_uuid(),
  skill_id    uuid not null references skills(id) on delete cascade,
  decision    text not null check (decision in ('approved', 'revoked')),
  reviewer    text not null default 'trilok',
  note        text,
  created_at  timestamptz not null default now()
);

create index if not exists skill_reviews_skill_idx on skill_reviews (skill_id, created_at desc);

-- 3 ---------------------------------------------------------------------

create table if not exists procedure_provenance (
  root        text not null check (root in ('repo', 'runtime')),
  path        text not null,               -- relative to the root, forward slashes
  provenance  text not null default 'unknown'
              check (provenance in ('user_authored', 'external_internet', 'agent_reflection', 'unknown')),
  source_ref  text,
  note        text,
  created_at  timestamptz not null default now(),
  primary key (root, path)
);

insert into procedure_provenance (root, path, provenance, source_ref, note) values
  ('repo', 'cost/prompt-caching-system-prompt.md', 'unknown', null,
   'The file says it was adapted from https://youtu.be/demo1, which looks like a placeholder; its real source is not recorded'),
  ('repo', 'sourcing/alibaba_supplier_eval.md', 'unknown', null,
   'No source recorded'),
  ('runtime', 'agents/multi-agent-review-orchestration.md', 'external_internet',
   'https://www.youtube.com/watch?v=YG4t7aMY81c', 'Adopted from an ingested video; the source is the file''s own trailer'),
  ('runtime', 'agents/observability-run-log.md', 'external_internet',
   'https://www.youtube.com/watch?v=o4KZH_KSqYQ', 'Adopted from an ingested video; the source is the file''s own trailer')
on conflict (root, path) do nothing;
