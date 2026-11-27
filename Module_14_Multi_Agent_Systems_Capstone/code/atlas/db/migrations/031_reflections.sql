-- 031: self-written skills.
--
-- reflections: which runs the nightly reflection has already read, and
-- what it made of each (a drafted learning on the Learn tab, or nothing).
-- One row per run, so a run is reflected on once however many nights pass.

create table if not exists reflections (
  episode_id    uuid primary key references episodes(id) on delete cascade,
  candidate_id  uuid references learning_candidates(id) on delete set null,
  worth         boolean not null default false,
  note          text,
  created_at    timestamptz not null default now()
);

create index if not exists reflections_created_idx on reflections (created_at desc);
