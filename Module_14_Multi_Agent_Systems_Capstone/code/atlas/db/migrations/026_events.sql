-- 026: the event stream behind mission control.
--
-- events  one row per thing Atlas does, the instant it does it: a run
--         starting, a tool call, an approval asked or decided, a push
--         sent, a screenshot taken while filling a form, a night-shift
--         task finishing. Written by every process and announced with
--         NOTIFY on the same channel, so the web app can show it live and
--         the morning replay can rebuild the night from it. Small rows:
--         ids and short text; the trace view holds the detail.

create table if not exists events (
  id          bigserial primary key,
  at          timestamptz not null default now(),
  kind        text not null,
  agent       text,
  episode_id  uuid,
  title       text not null default '',
  detail      text not null default '',
  data        jsonb not null default '{}'::jsonb
);

create index if not exists events_at_idx on events (at desc);
create index if not exists events_episode_idx on events (episode_id) where episode_id is not null;
