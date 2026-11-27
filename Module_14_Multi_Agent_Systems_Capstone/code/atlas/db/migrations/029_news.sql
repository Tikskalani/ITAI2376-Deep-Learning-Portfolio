-- 029: the morning AI brief.
--
-- news_items holds what the trusted feeds (config/news.yaml) said, one row
-- per story, so a story is read once however many mornings its feed keeps
-- listing it. The handful picked for a brief carry why they matter and the
-- brief they went into; the Learn tab shows those. The briefs table learns
-- a fourth kind.

create table if not exists news_items (
  id            uuid primary key default gen_random_uuid(),
  source        text not null,
  external_id   text not null,
  title         text not null,
  url           text,
  summary       text,
  published_at  timestamptz,
  fetched_at    timestamptz not null default now(),
  picked_at     timestamptz,
  rank          integer,
  why           text,
  brief_id      uuid references briefs(id) on delete set null,
  unique (source, external_id)
);

create index if not exists news_items_fetched_idx on news_items (fetched_at desc);
create index if not exists news_items_picked_idx on news_items (picked_at desc) where picked_at is not null;

alter table briefs drop constraint if exists briefs_kind_check;
alter table briefs add constraint briefs_kind_check check (kind in ('morning', 'evening', 'weekly', 'ai_news'));
