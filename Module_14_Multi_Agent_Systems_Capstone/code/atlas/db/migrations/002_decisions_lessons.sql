-- Atlas Migration 002 — decision journal and lessons
-- Postgres 16 + pgvector. Single-user, no tenancy.
--
-- Two tables that make Atlas compound rather than just accumulate:
--   decisions — a decision journal. Every real call is recorded with its
--               rationale and expected outcome, then revisited later so
--               Atlas can tell Trilok whether the call was right.
--   lessons   — distilled, embedded "what we learned" statements that
--               shape future agent runs.
--
-- Idempotent: `if not exists` on every object so re-runs are safe.

-- =====================================================
-- DECISION JOURNAL
-- =====================================================

create table if not exists decisions (
  id uuid primary key default gen_random_uuid(),
  summary text not null,            -- what was decided, one line
  rationale text,                   -- why it was decided that way
  expected_outcome text,            -- what we expect to happen
  made_by text default 'owner',     -- 'owner' or an agent name
  domain text,                      -- side_shop, money, property, etc.
  status text default 'open',       -- open, resolved
  outcome text,                     -- what actually happened (filled later)
  assessment text,                  -- right, wrong, mixed, unclear
  decided_at timestamptz default now(),
  review_at date,                   -- when to revisit this decision
  resolved_at timestamptz,
  goal_id uuid references goals(id) on delete set null,
  episode_id uuid references episodes(id) on delete set null,
  metadata jsonb default '{}'
);

create index if not exists idx_decisions_status on decisions(status);
create index if not exists idx_decisions_domain on decisions(domain);
create index if not exists idx_decisions_review
  on decisions(review_at) where status = 'open';

-- =====================================================
-- LESSONS
-- Distilled, embedded "what we learned" statements.
-- =====================================================

create table if not exists lessons (
  id uuid primary key default gen_random_uuid(),
  content text not null,            -- the lesson, one self-contained statement
  domain text,
  source_episode uuid references episodes(id) on delete set null,
  embedding vector(768),            -- nomic-embed-text, for retrieval
  created_at timestamptz default now(),
  metadata jsonb default '{}'
);

create index if not exists idx_lessons_embedding
  on lessons using hnsw (embedding vector_cosine_ops);
create index if not exists idx_lessons_domain on lessons(domain);
