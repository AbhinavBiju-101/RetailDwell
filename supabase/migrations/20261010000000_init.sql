-- Dwell-Time & Bottleneck Mapper: schema, row level security and storage buckets.
-- Run in the Supabase SQL editor, or with the Supabase CLI: `supabase db push`.
--
-- Access model
--   owner  accounts: read and write their own stores, layouts, datasets and API keys
--   demo   account : read-only access to the shared demo stores (is_demo = true)
--   The Flask server uses the service-role key only for ingest, seeding and files.

create extension if not exists pgcrypto;

-- ------------------------------------------------------------------ tables
create table if not exists public.profiles (
  id           uuid primary key references auth.users (id) on delete cascade,
  email        text,
  display_name text,
  role         text not null default 'owner' check (role in ('owner', 'demo')),
  created_at   timestamptz not null default now()
);

create table if not exists public.stores (
  id          uuid primary key default gen_random_uuid(),
  owner_id    uuid references public.profiles (id) on delete cascade,   -- null for shared demo stores
  slug        text unique,                                              -- readable id for demo stores
  name        text not null check (char_length(name) between 1 and 80),
  kind        text not null default 'Retail',
  blurb       text,
  width_m     numeric not null check (width_m > 0 and width_m <= 500),
  height_m    numeric not null check (height_m > 0 and height_m <= 500),
  window_s    integer not null default 300 check (window_s between 60 and 3600),
  hero_zone   text,
  plan_path   text,                                                     -- floor plan image in the `plans` bucket
  is_demo     boolean not null default false,
  created_at  timestamptz not null default now(),
  check (is_demo or owner_id is not null)
);
create index if not exists stores_owner_idx on public.stores (owner_id);

-- A layout is one arrangement of the floor: zones are {"Name": [x0, y0, x1, y1, "type"]} in metres.
create table if not exists public.layouts (
  id          uuid primary key default gen_random_uuid(),
  store_id    uuid not null references public.stores (id) on delete cascade,
  key         text not null check (key ~ '^[A-Za-z0-9_-]{1,8}$'),
  label       text not null,
  zones       jsonb not null default '{}'::jsonb,
  created_at  timestamptz not null default now(),
  unique (store_id, key)
);

-- A dataset is one recording of anonymous tracks for a layout.
-- source 'simulated' and 'upload' keep a CSV in the `tracks` bucket; 'ingest' keeps rows in ingest_points.
create table if not exists public.datasets (
  id            uuid primary key default gen_random_uuid(),
  layout_id     uuid not null references public.layouts (id) on delete cascade,
  label         text not null,
  source        text not null check (source in ('simulated', 'upload', 'ingest')),
  storage_path  text,
  n_points      integer not null default 0,
  n_tracks      integer not null default 0,
  observed_on   date,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  unique (layout_id, label)
);
create index if not exists datasets_layout_idx on public.datasets (layout_id, created_at desc);

-- Live points posted by a store's agents. Only these four fields exist, by design:
-- no images, no identifiers, nothing that could single a person out.
create table if not exists public.ingest_points (
  id          bigint generated always as identity primary key,
  dataset_id  uuid not null references public.datasets (id) on delete cascade,
  track_id    bigint not null,
  t           double precision not null,
  x           real not null,
  y           real not null
);
create index if not exists ingest_points_ds_idx on public.ingest_points (dataset_id, track_id, t);

-- Only a SHA-256 hash of each key is stored. The plain key is shown once, when it is created.
create table if not exists public.api_keys (
  id            uuid primary key default gen_random_uuid(),
  store_id      uuid not null references public.stores (id) on delete cascade,
  label         text not null default 'Camera agent',
  prefix        text not null,
  key_hash      text not null unique,
  created_at    timestamptz not null default now(),
  last_used_at  timestamptz,
  revoked_at    timestamptz
);
create index if not exists api_keys_store_idx on public.api_keys (store_id);

-- ------------------------------------------------------------------ helpers
create or replace function public.is_demo_user() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from public.profiles p where p.id = auth.uid() and p.role = 'demo');
$$;

create or replace function public.can_read_store(sid uuid) returns boolean
language sql stable security definer set search_path = public as $$
  select exists (
    select 1 from public.stores s
    where s.id = sid and (s.owner_id = auth.uid() or (s.is_demo and public.is_demo_user()))
  );
$$;

create or replace function public.owns_store(sid uuid) returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from public.stores s where s.id = sid and s.owner_id = auth.uid() and not s.is_demo);
$$;

-- every new auth user gets an owner profile; the demo role is set by seed_demo.py with the service key
create or replace function public.handle_new_user() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  insert into public.profiles (id, email, display_name, role)
  values (new.id, new.email,
          coalesce(new.raw_user_meta_data ->> 'display_name', split_part(new.email, '@', 1)), 'owner')
  on conflict (id) do nothing;
  return new;
end $$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created after insert on auth.users
  for each row execute function public.handle_new_user();

-- ------------------------------------------------------------------ row level security
alter table public.profiles      enable row level security;
alter table public.stores        enable row level security;
alter table public.layouts       enable row level security;
alter table public.datasets      enable row level security;
alter table public.ingest_points enable row level security;
alter table public.api_keys      enable row level security;

drop policy if exists profiles_read_own on public.profiles;
create policy profiles_read_own on public.profiles for select to authenticated using (id = auth.uid());

drop policy if exists stores_read on public.stores;
create policy stores_read on public.stores for select to authenticated
  using (owner_id = auth.uid() or (is_demo and public.is_demo_user()));
drop policy if exists stores_insert on public.stores;
create policy stores_insert on public.stores for insert to authenticated
  with check (owner_id = auth.uid() and not is_demo and not public.is_demo_user());
drop policy if exists stores_update on public.stores;
create policy stores_update on public.stores for update to authenticated
  using (owner_id = auth.uid() and not is_demo) with check (owner_id = auth.uid() and not is_demo);
drop policy if exists stores_delete on public.stores;
create policy stores_delete on public.stores for delete to authenticated
  using (owner_id = auth.uid() and not is_demo);

drop policy if exists layouts_read on public.layouts;
create policy layouts_read on public.layouts for select to authenticated using (public.can_read_store(store_id));
drop policy if exists layouts_write on public.layouts;
create policy layouts_write on public.layouts for all to authenticated
  using (public.owns_store(store_id)) with check (public.owns_store(store_id));

drop policy if exists datasets_read on public.datasets;
create policy datasets_read on public.datasets for select to authenticated
  using (exists (select 1 from public.layouts l where l.id = layout_id and public.can_read_store(l.store_id)));
drop policy if exists datasets_write on public.datasets;
create policy datasets_write on public.datasets for all to authenticated
  using (exists (select 1 from public.layouts l where l.id = layout_id and public.owns_store(l.store_id)))
  with check (exists (select 1 from public.layouts l where l.id = layout_id and public.owns_store(l.store_id)));

-- ingest_points: no policies for browsers or signed-in users; only the server (service role) reads and writes it.

drop policy if exists api_keys_owner on public.api_keys;
create policy api_keys_owner on public.api_keys for all to authenticated
  using (public.owns_store(store_id)) with check (public.owns_store(store_id));

-- ------------------------------------------------------------------ storage (private buckets, server access only)
insert into storage.buckets (id, name, public, file_size_limit)
values ('tracks', 'tracks', false, 52428800), ('plans', 'plans', false, 5242880)
on conflict (id) do nothing;
