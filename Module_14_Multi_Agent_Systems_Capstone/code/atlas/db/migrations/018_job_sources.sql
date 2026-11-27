-- 018: Workable boards join Greenhouse, Lever and Ashby (Hugging Face and
-- other companies post there).

alter table job_postings drop constraint if exists job_postings_source_check;
alter table job_postings add constraint job_postings_source_check
  check (source in ('greenhouse', 'lever', 'ashby', 'workable', 'manual'));

create index if not exists job_postings_unscored_idx on job_postings (first_seen_at desc) where score is null;
