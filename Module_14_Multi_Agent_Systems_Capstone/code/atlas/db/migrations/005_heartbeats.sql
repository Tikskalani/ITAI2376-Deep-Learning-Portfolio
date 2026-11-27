-- Atlas Migration 005 — system heartbeats
-- Postgres 16 + pgvector. Single-user, no tenancy.
--
-- One small table the executive loop and the brief jobs upsert into
-- whenever they complete a cycle. The health alerter polls it every
-- few minutes and pings Trilok over Telegram when something has not
-- ticked in a while, so silent breakage does not sit unnoticed.
--
-- Idempotent: `if not exists` on every object so re-runs are safe.

create table if not exists system_heartbeats (
  kind text primary key,                -- 'loop/executive', 'brief/morning', etc.
  last_at timestamptz not null default now(),
  metadata jsonb default '{}'
);
