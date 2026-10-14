-- Recording start time and store timezone: real clock time for time-of-day findings, and real timestamps in sales exports.
-- Safe to run more than once. The new columns inherit the existing row-level security on stores and datasets.
alter table public.stores   add column if not exists timezone   text not null default 'UTC';
alter table public.datasets add column if not exists started_at timestamptz;
