-- Atlas Memory Schema -- Migration 008_discovery
-- Discovery & learning loop for the news_discovery agent.
--
-- Two additive tables (nothing altered or dropped):
--   discovery_sources    trusted feeds (YouTube channels) the nightly scan sweeps
--   learning_candidates  vetted, sales-stripped learnings awaiting Trilok's review
--
-- Idempotent: `if not exists` on every table and index, safe to re-run.

-- =====================================================
-- Trusted sources the nightly scan sweeps.
-- =====================================================
create table if not exists discovery_sources (
  id              uuid primary key default gen_random_uuid(),
  kind            text not null default 'youtube_channel',  -- youtube_channel (more later)
  handle          text not null,           -- what Trilok gave: @handle, channel URL, or channel_id
  channel_id      text,                    -- resolved YouTube channel_id, used to build the RSS feed
  label           text,                    -- human label for digests
  active          boolean not null default true,
  last_scanned_at timestamptz,             -- newest upload seen as of the last sweep
  created_at      timestamptz not null default now(),
  unique (kind, handle)
);

create index if not exists idx_discovery_sources_active on discovery_sources(active);

-- =====================================================
-- Vetted learnings awaiting review. The queue behind the
-- nightly digest. status walks:
--   proposed -> pursuing -> drafted -> adopted | rejected
-- =====================================================
create table if not exists learning_candidates (
  id           uuid primary key default gen_random_uuid(),
  source_type  text not null,              -- youtube, instagram, manual
  source_url   text,
  creator      text,
  title        text,
  learning     text not null,              -- the extracted, sales-stripped takeaway
  novelty      real,                       -- 0..1, how new vs. what Atlas already knows
  relevance    real,                       -- 0..1, fit to Atlas / Trilok's priorities
  credibility  real,                       -- 0..1, how much to trust the claim
  verdict      text,                       -- keep | skip, the vetting call
  rationale    text,                       -- one line: why keep or skip
  status       text not null default 'proposed',
  dedup_key    text,                       -- soft-dedup against prior candidates
  source_id    uuid references discovery_sources(id) on delete set null,
  created_at   timestamptz not null default now(),
  reviewed_at  timestamptz,
  metadata     jsonb not null default '{}'
);

create index if not exists idx_learning_candidates_status on learning_candidates(status);
create index if not exists idx_learning_candidates_dedup  on learning_candidates(dedup_key);
