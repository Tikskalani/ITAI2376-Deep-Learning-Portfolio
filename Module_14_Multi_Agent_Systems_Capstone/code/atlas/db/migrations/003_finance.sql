-- Atlas Migration 003 — CFO finance: statement imports, transactions, bills
-- Postgres 16 + pgvector. Single-user, no tenancy.
--
-- Three tables behind the CFO agent:
--   statement_imports — one row per imported statement CSV. content_hash
--                       is unique, so re-importing the same file is caught
--                       and the month's numbers never double-count.
--   transactions      — every line from every imported statement, signed
--                       (negative is money out). category and stream are
--                       filled in by the CFO after import.
--   recurring_bills   — the bills the CFO watches: ACA, insurance, SaaS,
--                       and so on, with their cadence and typical amount.
--
-- Idempotent: `if not exists` on every object so re-runs are safe.

-- =====================================================
-- STATEMENT IMPORTS
-- =====================================================

create table if not exists statement_imports (
  id uuid primary key default gen_random_uuid(),
  account text not null,            -- account label, e.g. chase_checking, amex
  source_file text not null,        -- the file imported
  content_hash text not null unique,-- sha256 of the file; the re-import guard
  row_count int not null default 0,
  imported_at timestamptz default now(),
  metadata jsonb default '{}'
);

create index if not exists idx_statement_imports_account
  on statement_imports(account);

-- =====================================================
-- TRANSACTIONS
-- =====================================================

create table if not exists transactions (
  id uuid primary key default gen_random_uuid(),
  import_id uuid references statement_imports(id) on delete cascade,
  account text not null,
  posted date not null,
  description text not null,
  amount numeric(14,2) not null,    -- signed: negative is money out
  category text,                    -- e.g. groceries, fuel; null until set
  stream text,                      -- w2, side_shop, tutoring, household
  created_at timestamptz default now(),
  metadata jsonb default '{}'       -- the raw CSV row, kept for audit
);

create index if not exists idx_transactions_posted on transactions(posted);
create index if not exists idx_transactions_account on transactions(account);
create index if not exists idx_transactions_stream on transactions(stream);
create index if not exists idx_transactions_category on transactions(category);

-- =====================================================
-- RECURRING BILLS
-- =====================================================

create table if not exists recurring_bills (
  id uuid primary key default gen_random_uuid(),
  name text not null,               -- ACA premium, auto insurance, AWS
  amount numeric(14,2) not null,    -- typical amount, positive magnitude
  cadence text not null default 'monthly',  -- weekly, monthly, quarterly, annual
  due_day int,                      -- day of the month it is typically due
  account text,                     -- which account it hits, optional
  stream text,                      -- expense bucket, optional
  status text not null default 'active',  -- active, paused, ended
  last_seen date,                   -- last date a matching charge was found
  notes text,
  created_at timestamptz default now(),
  metadata jsonb default '{}'
);

create index if not exists idx_recurring_bills_status
  on recurring_bills(status);
