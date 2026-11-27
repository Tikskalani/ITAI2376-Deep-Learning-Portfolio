-- 016: an episode records what came back, not only what was asked.
--
-- Before this, a run was stored as "<agent> run: <prompt>" with an outcome
-- like "stop_reason=end_turn, turns=2", so the feedback buttons and the
-- learning loop rated prompts, not results. status is the verdict a
-- machine can count; result is the start of the final answer.

alter table episodes add column if not exists status text
  check (status in ('ok', 'incomplete', 'failed'));
alter table episodes add column if not exists result text;

-- Backfill the verdict from the old free-text outcome.
update episodes set status = case
    when outcome like 'error:%' then 'failed'
    when outcome like 'stop_reason=end_turn%' then 'ok'
    when outcome like 'stop_reason=%' then 'incomplete'
  end
where status is null and outcome is not null;

create index if not exists episodes_status_idx on episodes (status, started_at desc);
