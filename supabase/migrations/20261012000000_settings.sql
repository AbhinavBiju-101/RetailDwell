-- Per-store detection settings (staff, parties, stitching, sales matching): only values that differ from the defaults are stored.
-- Safe to run more than once. The column inherits the existing row-level security on stores.
alter table public.stores add column if not exists settings jsonb not null default '{}'::jsonb;
