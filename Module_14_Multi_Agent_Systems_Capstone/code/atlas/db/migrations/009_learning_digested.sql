-- Atlas Memory Schema -- Migration 009_learning_digested
-- Track when a learning candidate was sent in a nightly digest, so the
-- digest does not re-send the same un-actioned candidate every night.
-- Idempotent: safe to re-run.

alter table learning_candidates add column if not exists digested_at timestamptz;

create index if not exists idx_learning_candidates_undigested
  on learning_candidates (status) where digested_at is null;
