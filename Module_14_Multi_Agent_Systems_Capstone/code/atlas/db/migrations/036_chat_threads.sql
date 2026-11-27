-- 036_chat_threads.sql -- the Chat channel keeps its conversations.
--
-- chat_threads   one conversation with the CEO; a thread continues until
--                six quiet hours pass or he starts a new one (core/chat_history.py).
-- chat_messages  what he typed (role 'you') and the CEO's reply (role 'ceo'),
--                in order, with the run that answered.
--
-- Idempotent: safe to apply twice.

create table if not exists chat_threads (
  id          uuid primary key default gen_random_uuid(),
  title       text not null default '',
  started_at  timestamptz not null default now(),
  last_at     timestamptz not null default now()
);

create table if not exists chat_messages (
  id          bigserial primary key,
  thread_id   uuid not null references chat_threads(id) on delete cascade,
  role        text not null check (role in ('you', 'ceo')),
  body        text not null,
  episode_id  uuid,
  created_at  timestamptz not null default now()
);

create index if not exists chat_messages_thread_idx on chat_messages (thread_id, id);
create index if not exists chat_threads_last_idx on chat_threads (last_at desc);
