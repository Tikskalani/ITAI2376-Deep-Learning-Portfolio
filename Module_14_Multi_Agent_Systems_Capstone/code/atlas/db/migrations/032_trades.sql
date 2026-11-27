-- 032: trades, read-only.
--
-- Robinhood has no personal API and Atlas will never hold the account.
-- What it holds is the transaction export he downloads: one row per
-- activity (a buy, a sell, a dividend, a deposit, a fee). Cash movements
-- also land in transactions, so cash flow sees them; the trades table keeps
-- the instrument, quantity and price so positions and realized gains can
-- be recomputed from the record at any time.

create table if not exists trades (
  id           uuid primary key default gen_random_uuid(),
  import_id    uuid references statement_imports(id) on delete cascade,
  account      text not null,
  trade_date   date not null,
  settle_date  date,
  instrument   text,
  description  text not null default '',
  code         text not null,              -- Robinhood's Trans Code: Buy, Sell, CDIV, ACH, AFEE, GOLD, SLIP, INT ...
  kind         text not null,              -- buy, sell, dividend, deposit, withdrawal, fee, interest, other
  quantity     numeric(18, 6),
  price        numeric(14, 4),
  amount       numeric(14, 2) not null,    -- signed: negative is money out of the account
  metadata     jsonb not null default '{}',
  created_at   timestamptz not null default now()
);

create index if not exists trades_account_date_idx on trades (account, trade_date);
create index if not exists trades_instrument_idx on trades (instrument) where instrument is not null;
