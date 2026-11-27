-- Atlas Migration 007 -- per-agent monthly budget caps
-- Caps are optional. An agent without a row in this table runs only
-- against the global daily cap. Setting a row adds a second gate that
-- blocks dispatch when that agent's month-to-date spend exceeds the
-- cap. The reset is monthly; spending resets when the calendar month
-- rolls over.

create table if not exists agent_caps (
  agent_name text primary key,
  monthly_usd_cap numeric not null check (monthly_usd_cap >= 0),
  updated_at timestamptz default now()
);
