-- ============================================================================
-- Doxari upgrade — 2026-11-04
-- Adds folders, recoverable trash, sharing, activity history and preferences.
--
-- Safe to run on a live deployment:
--   * no table is dropped, no row is deleted, no existing column is altered in a
--     destructive way
--   * every statement is idempotent, so running it twice is harmless
--   * your existing documents, storage objects and accounts are untouched
--
-- How to apply: Supabase dashboard -> SQL Editor -> New query -> paste -> Run.
-- ============================================================================

-- ---------------------------------------------------------------- helpers
create extension if not exists "pgcrypto";

create or replace function public.doxari_touch_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = timezone('utc', now());
  return new;
end;
$$;

-- ============================================================ 1. documents
-- The original table stored only filename + storage_path. These columns are
-- additive and nullable, so old rows keep working exactly as before.
alter table if exists public.documents add column if not exists size bigint;
alter table if exists public.documents add column if not exists mime_type text;
alter table if exists public.documents add column if not exists description text;
alter table if exists public.documents add column if not exists tags text[] default '{}'::text[];
alter table if exists public.documents add column if not exists deleted_at timestamptz;
alter table if exists public.documents add column if not exists last_accessed_at timestamptz;
alter table if exists public.documents add column if not exists updated_at timestamptz default timezone('utc', now());
alter table if exists public.documents add column if not exists folder_id uuid;
alter table if exists public.documents add column if not exists content_text text;

create index if not exists documents_user_created_idx on public.documents (user_id, created_at desc);
create index if not exists documents_user_folder_idx on public.documents (user_id, folder_id);
create index if not exists documents_user_deleted_idx on public.documents (user_id, deleted_at);
create index if not exists documents_user_filename_idx on public.documents (user_id, lower(filename));

-- ============================================================== 2. folders
create table if not exists public.folders (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users (id) on delete cascade,
  name text not null check (char_length(name) between 1 and 60),
  parent_id uuid references public.folders (id) on delete set null,
  color text,
  icon text,
  description text,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now())
);

create index if not exists folders_user_idx on public.folders (user_id, name);
create index if not exists folders_parent_idx on public.folders (parent_id);

-- Guard against a folder pointing at itself / a cycle deeper than one level.
create or replace function public.doxari_folder_no_cycle()
returns trigger
language plpgsql
as $$
declare
  cursor_id uuid := new.parent_id;
  guard int := 0;
begin
  while cursor_id is not null and guard < 32 loop
    if cursor_id = new.id then
      raise exception 'A folder cannot be nested inside itself';
    end if;
    select parent_id into cursor_id from public.folders where id = cursor_id;
    guard := guard + 1;
  end loop;
  return new;
end;
$$;

drop trigger if exists folders_no_cycle on public.folders;
create trigger folders_no_cycle
  before insert or update of parent_id on public.folders
  for each row execute function public.doxari_folder_no_cycle();

-- documents.folder_id -> folders.id (set null on folder delete so files survive)
do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'documents_folder_fk'
  ) then
    alter table public.documents
      add constraint documents_folder_fk
      foreign key (folder_id) references public.folders (id) on delete set null;
  end if;
end;
$$;

-- ======================================================= 3. document_shares
create table if not exists public.document_shares (
  id uuid primary key default gen_random_uuid(),
  document_id uuid not null references public.documents (id) on delete cascade,
  owner_id uuid not null references auth.users (id) on delete cascade,
  shared_with_id uuid references auth.users (id) on delete cascade,
  shared_with_email text not null,
  permission text not null default 'view' check (permission in ('view', 'download')),
  revoked boolean not null default false,
  created_at timestamptz not null default timezone('utc', now())
);

create unique index if not exists document_shares_unique_active
  on public.document_shares (document_id, shared_with_email)
  where revoked = false;
create index if not exists document_shares_owner_idx on public.document_shares (owner_id, created_at desc);
create index if not exists document_shares_recipient_idx on public.document_shares (shared_with_id);
create index if not exists document_shares_document_idx on public.document_shares (document_id);

-- ========================================================= 4. share_links
create table if not exists public.share_links (
  id uuid primary key default gen_random_uuid(),
  document_id uuid not null references public.documents (id) on delete cascade,
  owner_id uuid not null references auth.users (id) on delete cascade,
  token text not null unique,
  permission text not null default 'view' check (permission in ('view', 'download')),
  expires_at timestamptz,
  revoked boolean not null default false,
  created_at timestamptz not null default timezone('utc', now())
);

create index if not exists share_links_token_idx on public.share_links (token);
create index if not exists share_links_document_idx on public.share_links (document_id);
create index if not exists share_links_owner_idx on public.share_links (owner_id);

-- ======================================================== 5. activity_log
create table if not exists public.activity_log (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users (id) on delete cascade,
  action text not null,
  document_id uuid,
  document_name text,
  folder_id uuid,
  meta jsonb default '{}'::jsonb,
  created_at timestamptz not null default timezone('utc', now())
);

create index if not exists activity_log_user_idx on public.activity_log (user_id, created_at desc);
create index if not exists activity_log_document_idx on public.activity_log (document_id);

-- ==================================================== 6. user_preferences
create table if not exists public.user_preferences (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null unique references auth.users (id) on delete cascade,
  theme text not null default 'system' check (theme in ('light', 'dark', 'system')),
  view_mode text not null default 'grid' check (view_mode in ('grid', 'list')),
  notifications boolean not null default true,
  onboarded boolean not null default false,
  ai_consent boolean not null default false,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now())
);

-- ============================================== 7. updated_at maintenance
drop trigger if exists documents_touch_updated_at on public.documents;
create trigger documents_touch_updated_at
  before update on public.documents
  for each row execute function public.doxari_touch_updated_at();

drop trigger if exists folders_touch_updated_at on public.folders;
create trigger folders_touch_updated_at
  before update on public.folders
  for each row execute function public.doxari_touch_updated_at();

drop trigger if exists user_preferences_touch_updated_at on public.user_preferences;
create trigger user_preferences_touch_updated_at
  before update on public.user_preferences
  for each row execute function public.doxari_touch_updated_at();

-- ============================================================= 8. RLS
-- Ownership is explicit. Nothing is readable across accounts, except a
-- document that was deliberately shared with the reader.
alter table public.documents enable row level security;
alter table public.folders enable row level security;
alter table public.document_shares enable row level security;
alter table public.share_links enable row level security;
alter table public.activity_log enable row level security;
alter table public.user_preferences enable row level security;

-- ---- documents
drop policy if exists "documents owner all" on public.documents;
create policy "documents owner all" on public.documents
  for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

drop policy if exists "documents shared read" on public.documents;
create policy "documents shared read" on public.documents
  for select
  using (
    exists (
      select 1 from public.document_shares s
      where s.document_id = documents.id
        and s.shared_with_id = auth.uid()
        and s.revoked = false
    )
  );

-- ---- folders
drop policy if exists "folders owner all" on public.folders;
create policy "folders owner all" on public.folders
  for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

-- ---- document_shares (owner manages, recipient can see their own rows)
drop policy if exists "shares owner all" on public.document_shares;
create policy "shares owner all" on public.document_shares
  for all
  using (auth.uid() = owner_id)
  with check (auth.uid() = owner_id);

drop policy if exists "shares recipient read" on public.document_shares;
create policy "shares recipient read" on public.document_shares
  for select
  using (auth.uid() = shared_with_id);

-- ---- share_links: only the owner may read or change these rows.
-- Public link visitors are served by the backend, which validates the token,
-- the expiry and the permission before streaming a byte.
drop policy if exists "links owner all" on public.share_links;
create policy "links owner all" on public.share_links
  for all
  using (auth.uid() = owner_id)
  with check (auth.uid() = owner_id);

-- ---- activity_log
drop policy if exists "activity owner all" on public.activity_log;
create policy "activity owner all" on public.activity_log
  for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

-- ---- user_preferences
drop policy if exists "preferences owner all" on public.user_preferences;
create policy "preferences owner all" on public.user_preferences
  for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

-- =========================================================== 9. storage
-- The existing private bucket keeps its policy. This just guarantees the
-- bucket is not public and that objects can only be touched by their owner,
-- whose files live under a <user_id>/ prefix.
update storage.buckets set public = false where id = 'document';

drop policy if exists "doxari objects owner only" on storage.objects;
create policy "doxari objects owner only" on storage.objects
  for all
  using (
    bucket_id = 'document'
    and auth.uid()::text = (storage.foldername(name))[1]
  )
  with check (
    bucket_id = 'document'
    and auth.uid()::text = (storage.foldername(name))[1]
  );

-- ============================================================== 10. done
-- Existing rows get sensible defaults so nothing looks broken after upgrade.
update public.documents set tags = '{}'::text[] where tags is null;
update public.documents set updated_at = coalesce(updated_at, created_at) where updated_at is null;
