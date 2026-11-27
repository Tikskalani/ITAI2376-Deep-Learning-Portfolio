-- 035: the privacy gate (core/privacy.py, docs/privacy.md).
--
-- signals.private marks a signal no model may read: its body is never stored
-- and its subject is "Private message (<rule label>)". private_rule is that
-- label (a name Trilok chose, never the address that matched). Existing rows
-- are untouched: they default to not private.
--
-- draft_email (client_delivery) creates a Gmail draft. It matched no approval
-- row, and an unmatched tool is auto, so it needs one: approve, failing closed
-- like every other outward tool. Idempotent.

alter table signals add column if not exists private boolean not null default false;
alter table signals add column if not exists private_rule text;

create index if not exists signals_private_idx on signals (occurred_at desc) where private;

insert into approval_policy (tool_pattern, gate, note) values
  ('*draft_email*', 'approve', 'Gmail drafts are approved first (privacy gate, 035)')
on conflict (tool_pattern) do nothing;
