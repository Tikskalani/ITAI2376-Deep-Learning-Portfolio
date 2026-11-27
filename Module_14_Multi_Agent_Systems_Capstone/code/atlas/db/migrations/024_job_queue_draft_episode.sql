-- 024: the apply queue remembers which run wrote each item's materials.
--
-- Every tailored resume and cover note in the queue was written by one
-- career-agent run. Keeping that run's episode id on the queue row lets the
-- Jobs card open the run's trace: "why these words?" shows the exact tool
-- calls, the playbook in force, what the model read, and what it cost.
-- A draft that failed keeps the id too, so "why did it fail?" has an answer.

alter table job_queue add column if not exists draft_episode_id uuid references episodes(id) on delete set null;

create index if not exists job_queue_draft_episode_idx on job_queue (draft_episode_id);
