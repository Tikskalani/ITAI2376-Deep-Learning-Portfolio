-- Atlas Migration 006 -- per-agent working context
-- Each row is a short "what I last did" note written by an agent when
-- its episode closes. On the next dispatch the most recent context for
-- that agent is prepended so specialists are not stateless.

create table if not exists agent_context (
  id uuid primary key default gen_random_uuid(),
  agent_name text not null,
  episode_id uuid references episodes(id) on delete set null,
  summary text not null,
  created_at timestamptz default now()
);

create index if not exists idx_agent_context_lookup
  on agent_context (agent_name, created_at desc);
