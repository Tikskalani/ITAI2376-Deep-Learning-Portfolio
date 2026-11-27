-- Atlas Memory Schema -- Migration 010_seen_videos
-- Dedup for the discovery scan. yt-dlp channel listing has no upload dates,
-- so instead of a time watermark we record every video we have already
-- vetted (kept or skipped) and skip it next sweep. Idempotent.

create table if not exists seen_videos (
  video_id  text primary key,
  source_id uuid references discovery_sources(id) on delete set null,
  seen_at   timestamptz not null default now()
);
