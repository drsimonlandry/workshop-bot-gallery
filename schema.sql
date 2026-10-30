-- Run once in the Supabase SQL editor (personal Supabase project).
create extension if not exists pgcrypto;

create table if not exists public.bots (
  id uuid primary key default gen_random_uuid(),
  name text not null check (char_length(name) between 1 and 80),
  creator_name text not null default '' check (char_length(creator_name) <= 80),
  description text not null default '' check (char_length(description) <= 500),
  agent_id uuid not null,
  api_url text not null,
  encrypted_pat text not null,
  manage_token_hash text not null,
  chat_count integer not null default 0,
  thumbs_up integer not null default 0,
  thumbs_down integer not null default 0,
  status text not null default 'active' check (status in ('active', 'unavailable', 'disabled')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (api_url, agent_id)
);

create table if not exists public.app_settings (
  key text primary key,
  value text not null
);

-- Only the server-side service-role key may touch these tables.
-- RLS with no policies denies the public anon/authenticated roles entirely.
alter table public.bots enable row level security;
alter table public.app_settings enable row level security;
revoke all on public.bots from anon, authenticated;
revoke all on public.app_settings from anon, authenticated;

-- Atomic counter updates (chat clicks and thumbs votes). Server-side only.
create or replace function public.bump_bot(p_bot uuid, p_chats int default 0, p_up int default 0, p_down int default 0)
returns void language sql as $$
  update public.bots
  set chat_count = greatest(chat_count + p_chats, 0),
      thumbs_up = greatest(thumbs_up + p_up, 0),
      thumbs_down = greatest(thumbs_down + p_down, 0)
  where id = p_bot;
$$;
revoke execute on function public.bump_bot(uuid, int, int, int) from public, anon, authenticated;
grant execute on function public.bump_bot(uuid, int, int, int) to service_role;
