-- Atlas Memory Schema -- Migration 012_notify
-- R1 Session A: the surface-agnostic notification layer that replaces Telegram.
--
--   push_subscriptions  one row per browser/PWA install that asked for push
--   notifications       everything Atlas has told the owner, with how it
--                       was delivered and whether it has been read; the
--                       PWA inbox reads this and the email fallback sweeps it
--   briefs              morning, evening and weekly briefs, stored before
--                       they are pushed so the PWA can show history
--
-- approvals gains decided_by and a link to the notification that carried
-- the card. telegram_message_id stays (never edit a committed migration).

create table if not exists push_subscriptions (
  id            uuid primary key default gen_random_uuid(),
  endpoint      text not null unique,
  p256dh        text not null,
  auth          text not null,
  user_agent    text,
  created_at    timestamptz not null default now(),
  last_used_at  timestamptz,
  failures      integer not null default 0
);

create table if not exists notifications (
  id             uuid primary key default gen_random_uuid(),
  kind           text not null,
  title          text not null,
  body           text not null default '',
  priority       text not null default 'normal'
                 check (priority in ('low', 'normal', 'high')),
  data           jsonb not null default '{}'::jsonb,
  created_at     timestamptz not null default now(),
  delivered_via  text[] not null default '{}',
  read_at        timestamptz
);

create index if not exists notifications_unread_idx
  on notifications (created_at desc) where read_at is null;

create table if not exists briefs (
  id               uuid primary key default gen_random_uuid(),
  kind             text not null check (kind in ('morning', 'evening', 'weekly')),
  body             text not null,
  created_at       timestamptz not null default now(),
  notification_id  uuid references notifications(id) on delete set null,
  read_at          timestamptz
);

create index if not exists briefs_kind_created_idx on briefs (kind, created_at desc);

alter table approvals add column if not exists decided_by text;
alter table approvals add column if not exists notification_id uuid
  references notifications(id) on delete set null;
