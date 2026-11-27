-- 027: recordings (the Record button).
--
-- recordings  one row per lecture, meeting or note Trilok recorded in the
--             PWA or uploaded from Voice Memos. The audio stays on the
--             laptop under data/recordings/ (gitignored); the transcript
--             comes from local faster-whisper; the notes and action items
--             from the agent that owns that kind of recording.
--
--   uploaded      the file is on disk
--   transcribing  faster-whisper is working (an hour of audio takes minutes)
--   transcribed   text is in; the agent is next
--   noting        the agent is writing the notes
--   done          notes and action items are ready
--   failed        `error` says what broke; Retry runs it again

create table if not exists recordings (
  id            uuid primary key default gen_random_uuid(),
  kind          text not null default 'lecture' check (kind in ('lecture', 'meeting', 'note')),
  title         text not null,
  path          text not null,
  bytes         bigint not null default 0,
  duration_s    integer,
  status        text not null default 'uploaded'
                check (status in ('uploaded', 'transcribing', 'transcribed', 'noting', 'done', 'failed')),
  project_id    uuid references projects(id) on delete set null,
  transcript    text,
  notes         text,
  action_items  jsonb not null default '[]'::jsonb,
  episode_id    uuid references episodes(id) on delete set null,
  error         text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

create index if not exists recordings_created_idx on recordings (created_at desc);
