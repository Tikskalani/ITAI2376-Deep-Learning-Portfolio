-- 028: the Planner. The deadlines table becomes the task list.
--
-- A task is a dated commitment with a title; it may carry a definition of
-- done (what finished looks like), a time of day, and the thing that put it
-- there (a recording's action item, a night task, an agent). A task with no
-- date yet is "someday": due_date was not null before, so quick-add had no
-- place for "look into the Tesla API" until a date existed.

alter table deadlines alter column due_date drop not null;
alter table deadlines add column if not exists due_at timestamptz;
alter table deadlines add column if not exists definition_of_done text;
alter table deadlines add column if not exists source_ref text;

create index if not exists idx_deadlines_project on deadlines (project_id);
create index if not exists idx_deadlines_source_ref on deadlines (source, source_ref) where source_ref is not null;
