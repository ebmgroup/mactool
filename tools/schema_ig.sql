-- IG Versionen: Instagram-Versionen, Handy-Bestand und Update-Auftraege.
--
-- Einmalig in der Kontroll-Supabase (fxreaveeihaawkusmybi) ausfuehren — dort, wo auch
-- mac_commands liegt. Mehrfach ausfuehrbar.
--
-- Zeiten stempelt die Datenbank selbst (Trigger): die Uhren der Macs gehen bewusst falsch.

-- ── Bereitgestellte Versionen ─────────────────────────────────────────
create table if not exists public.ig_versions (
  id            bigserial primary key,
  release       text        not null unique,     -- Ordner im Bucket apk
  package       text        not null default 'com.instagram.android',
  version_name  text        not null,
  version_code  bigint      not null,
  abi           text,
  file_name     text,
  bytes         bigint,
  sha256        text,
  status        text        not null default 'uploading',   -- uploading | ready
  is_standard   boolean     not null default false,
  uploaded_by   text,
  created_at    timestamptz not null default now(),
  note          text
);
-- Hoechstens eine Standard-Version
create unique index if not exists ig_versions_one_standard
  on public.ig_versions ((true)) where is_standard;

-- ── Bestand je Handy (meldet der Mac) ─────────────────────────────────
create table if not exists public.ig_phones (
  server_name   text        not null,
  serial        text        not null,
  name          text,
  state         text,                    -- device | unauthorized | offline …
  manufacturer  text,
  model         text,
  android       text,
  locale        text,
  error         text,
  packages      jsonb       not null default '{}'::jsonb,   -- paket -> {version_name, version_code, …}
  reported_at   timestamptz not null default now(),
  primary key (server_name, serial)
);

-- ── Update-Auftraege (Dashboard -> Mac) ───────────────────────────────
-- serial null = ganzer Mac; package null = alle Kopien des Handys (+ Original)
create table if not exists public.ig_update_jobs (
  id            bigserial primary key,
  server_name   text        not null,
  serial        text,
  package       text,
  version_id    bigint      references public.ig_versions(id),
  status        text        not null default 'queued',  -- queued | running | done | error | cancelled
  requested_by  text,
  created_at    timestamptz not null default now(),
  started_at    timestamptz,
  finished_at   timestamptz,
  progress      text,
  result        jsonb,
  error         text
);
create index if not exists ig_update_jobs_pending_idx
  on public.ig_update_jobs (server_name, status, created_at);

-- ── Zeitstempel aus der Datenbank ─────────────────────────────────────
create or replace function public.ig_stamp_phone() returns trigger language plpgsql as $$
begin
  new.reported_at := now();
  return new;
end $$;
drop trigger if exists ig_phones_stamp on public.ig_phones;
create trigger ig_phones_stamp before insert or update on public.ig_phones
  for each row execute function public.ig_stamp_phone();

create or replace function public.ig_stamp_job() returns trigger language plpgsql as $$
begin
  if new.status = 'running' and (tg_op = 'INSERT' or old.status <> 'running') then
    new.started_at := now();
  end if;
  if new.status in ('done', 'error', 'cancelled') and (tg_op = 'INSERT' or old.status not in ('done', 'error', 'cancelled')) then
    new.finished_at := now();
  end if;
  return new;
end $$;
drop trigger if exists ig_jobs_stamp on public.ig_update_jobs;
create trigger ig_jobs_stamp before insert or update on public.ig_update_jobs
  for each row execute function public.ig_stamp_job();

-- Nur der Service-Key (Macs, Edge Function) kommt heran
alter table public.ig_versions    enable row level security;
alter table public.ig_phones      enable row level security;
alter table public.ig_update_jobs enable row level security;
