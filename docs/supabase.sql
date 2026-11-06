-- Supabase 테이블 (설계서 6.3). 대시보드 SQL Editor에 붙여 넣고 한 번 실행한다. 여러 번 실행해도 된다.
-- 서버만 service_role 키로 접근한다. RLS는 켜 두고 공개 정책은 만들지 않는다 (anon·로그인 키로는 못 읽음).
-- 사용자 테이블은 따로 없다. auth.users를 참조하므로 계정을 지우면 행도 함께 지워진다.

create table if not exists public.profiles (
  user_id    uuid primary key references auth.users (id) on delete cascade,
  json       jsonb not null,
  updated_at timestamptz not null default now()
);

create table if not exists public.sessions (
  id         text primary key,
  user_id    uuid not null references auth.users (id) on delete cascade,
  title      text not null default '',
  stage      text not null default '',
  json       jsonb not null,
  updated_at timestamptz not null default now()
);
create index if not exists sessions_user_updated on public.sessions (user_id, updated_at desc);

-- 로그인 사용자의 작업 결과. 비로그인 결과는 서버 디스크(DB/runs/)에 24시간만 둔다.
create table if not exists public.runs (
  id            text primary key,
  owner_session text,
  user_id       uuid references auth.users (id) on delete cascade,
  json          jsonb not null,
  created_at    timestamptz not null default now()
);
create index if not exists runs_user_session on public.runs (user_id, owner_session);

-- 논문별 결과 캐시 (src/paper_cache.py). kind: abstract_card | studio_card, key: S2 paper_id.
-- 같은 논문은 Solar·Studio를 다시 부르지 않는다. 사용자와 무관한 공용 데이터라 user_id가 없다.
create table if not exists public.cache (
  kind       text not null,
  key        text not null,
  json       jsonb not null,
  created_at timestamptz not null default now(),
  primary key (kind, key)
);

alter table public.profiles enable row level security;
alter table public.sessions enable row level security;
alter table public.runs     enable row level security;
alter table public.cache    enable row level security;
