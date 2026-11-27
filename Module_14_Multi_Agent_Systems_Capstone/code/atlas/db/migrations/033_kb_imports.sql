-- 033: the knowledge base from Claude.
--
-- A claude.ai data export (conversations, projects, memories) is queued as
-- one import with one item per conversation or project. The worker reads
-- items a batch at a time with the scan model, writes what is durable
-- about him into facts and the profile, and keeps the count and the cost
-- here so the Connect tab can show the import moving and stop it at the
-- cap. An item is read once, however many times the same export is
-- dropped (ref is the conversation's own uuid).

create table if not exists kb_imports (
  id            uuid primary key default gen_random_uuid(),
  kind          text not null default 'claude_export',
  filename      text,
  status        text not null default 'queued'
                check (status in ('queued', 'running', 'paused', 'done', 'failed')),
  items         integer not null default 0,
  done          integer not null default 0,
  skipped       integer not null default 0,
  facts         integer not null default 0,
  notes         integer not null default 0,
  estimate_usd  numeric(10, 2),
  cap_usd       numeric(10, 2),
  cost_usd      numeric(10, 4) not null default 0,
  error         text,
  created_at    timestamptz not null default now(),
  started_at    timestamptz,
  finished_at   timestamptz,
  metadata      jsonb not null default '{}'
);

create table if not exists kb_items (
  id          uuid primary key default gen_random_uuid(),
  import_id   uuid not null references kb_imports(id) on delete cascade,
  kind        text not null,                  -- conversation, project, memory
  ref         text not null,                  -- the export's own uuid for the item
  title       text,
  chars       integer not null default 0,
  payload     jsonb not null default '{}',    -- the bounded text the model reads
  status      text not null default 'queued'
              check (status in ('queued', 'done', 'skipped', 'failed')),
  facts       integer not null default 0,
  error       text,
  created_at  timestamptz not null default now(),
  done_at     timestamptz,
  unique (kind, ref)
);

create index if not exists kb_items_import_status_idx on kb_items (import_id, status);
