-- 크레딧 원장과 결제 주문.
-- Supabase 대시보드의 SQL Editor에 붙여 넣어 실행한다(0001_init.sql 다음에). staging, production 에 각각 한 번씩.
--
-- 원장(credit_ledger)은 크레딧이 늘고 준 기록을 쌓기만 한다. 잔액은 그 사용자의 delta 합계다.
-- 고치거나 지우지 않는다. 잘못 뺐으면 되돌리는 줄(+)을 새로 쌓는다.
-- ref 를 유일하게 두어 같은 결제로 두 번 넣거나 같은 메시지로 두 번 빼는 일을 데이터베이스가 막는다.
--   signup:사용자ID / chat:요청ID / refund:chat:요청ID / order:주문번호
--
-- 0001 과 같이 RLS 를 켜고 정책은 만들지 않는다. 서버(secret key)만 읽고 쓴다.

create table if not exists public.credit_ledger (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users (id) on delete cascade,
  delta integer not null check (delta <> 0),
  reason text not null check (reason in ('signup', 'chat', 'refund', 'purchase')),
  ref text not null unique,
  created_at timestamptz not null default now()
);
create index if not exists credit_ledger_user_idx on public.credit_ledger (user_id, id);

create table if not exists public.orders (
  order_id text primary key,                 -- 서버가 만든 추측할 수 없는 무작위 값. 토스 멱등키로도 쓴다
  user_id uuid not null references auth.users (id) on delete cascade,
  product_id text not null,
  amount integer not null check (amount > 0),   -- 서버가 상품표에서 찾은 금액. 화면이 보낸 금액이 아니다
  credits integer not null check (credits > 0),
  status text not null default 'pending',     -- pending, done, failed, refunding, refunded (제약은 아래)
  payment_key text,
  fail_code text,
  approved_at timestamptz,
  refunded_at timestamptz,
  created_at timestamptz not null default now()
);
create index if not exists orders_user_idx on public.orders (user_id, created_at);

-- 이 파일을 이미 한 번 실행한 프로젝트에서도 다시 실행하면 맞춰지도록(환불 추가)
alter table public.orders add column if not exists refunded_at timestamptz;
alter table public.orders drop constraint if exists orders_status_check;
alter table public.orders add constraint orders_status_check
  check (status in ('pending', 'done', 'failed', 'refunding', 'refunded'));

alter table public.credit_ledger enable row level security;
alter table public.orders enable row level security;

-- 잔액. PostgREST 는 기본 설정에서 sum 을 못 쓰므로 함수로 둔다.
create or replace function public.credit_balance(p_user uuid)
returns bigint language sql stable security definer set search_path = public as $$
  select coalesce(sum(delta), 0)::bigint from public.credit_ledger where user_id = p_user;
$$;

-- 크레딧 빼기. 사용자마다 잠금을 걸고 잔액을 확인한 뒤 빼므로,
-- 잔액 1에서 요청 열 개가 동시에 와도 하나만 통과한다.
create or replace function public.spend_credit(p_user uuid, p_amount integer, p_reason text, p_ref text)
returns table (ok boolean, balance bigint) language plpgsql security definer set search_path = public as $$
declare
  current_balance bigint;
begin
  if p_amount is null or p_amount <= 0 then
    raise exception 'spend_credit: p_amount must be positive';  -- 음수로 부르면 크레딧이 늘어나므로 막는다
  end if;
  perform pg_advisory_xact_lock(hashtextextended(p_user::text, 0));
  select coalesce(sum(delta), 0) into current_balance from public.credit_ledger where user_id = p_user;
  if current_balance < p_amount then
    return query select false, current_balance;
    return;
  end if;
  insert into public.credit_ledger (user_id, delta, reason, ref) values (p_user, -p_amount, p_reason, p_ref);
  return query select true, current_balance - p_amount;
end;
$$;

-- 함수는 서버(secret key = service_role)만 부른다. 브라우저의 publishable key 로는 못 부르게 막는다.
revoke execute on function public.credit_balance(uuid) from public, anon, authenticated;
revoke execute on function public.spend_credit(uuid, integer, text, text) from public, anon, authenticated;
grant execute on function public.credit_balance(uuid) to service_role;
grant execute on function public.spend_credit(uuid, integer, text, text) to service_role;

-- 가입 무료 크레딧은 같은 IP 에서 하루(한국 날짜) p_limit 명까지만(2026-11-04 공격 2: 시크릿 창으로 게스트를
-- 새로 만들면 매번 30개를 받았다). IP 는 그대로 저장하지 않고 서버가 비밀값으로 섞은 해시만 받는다.
create table if not exists public.free_grants (
  user_id uuid primary key references auth.users (id) on delete cascade,
  ip_hash text not null,
  day date not null,
  granted boolean not null,
  created_at timestamptz not null default now()
);
create index if not exists free_grants_ip_day_idx on public.free_grants (ip_hash, day);
alter table public.free_grants enable row level security;

create or replace function public.grant_signup(p_user uuid, p_ip_hash text, p_amount integer, p_limit integer)
returns boolean language plpgsql security definer set search_path = public as $$
declare
  today date := (now() at time zone 'Asia/Seoul')::date;
  used integer;
  done boolean;
begin
  -- 같은 IP 의 게스트가 동시에 여러 명 와도 한도를 넘지 않게 IP 별로 잠근다
  perform pg_advisory_xact_lock(hashtextextended('free:' || p_ip_hash, 0));
  select granted into done from public.free_grants where user_id = p_user;
  if found then
    return done;
  end if;
  if exists (select 1 from public.credit_ledger where ref = 'signup:' || p_user::text) then
    -- 이 제한을 만들기 전에 이미 받은 사용자
    insert into public.free_grants (user_id, ip_hash, day, granted) values (p_user, p_ip_hash, today, true)
      on conflict (user_id) do nothing;
    return true;
  end if;
  select count(*) into used from public.free_grants where ip_hash = p_ip_hash and day = today and granted;
  if used >= p_limit then
    insert into public.free_grants (user_id, ip_hash, day, granted) values (p_user, p_ip_hash, today, false)
      on conflict (user_id) do nothing;
    return false;
  end if;
  insert into public.free_grants (user_id, ip_hash, day, granted) values (p_user, p_ip_hash, today, true)
    on conflict (user_id) do nothing;
  insert into public.credit_ledger (user_id, delta, reason, ref) values (p_user, p_amount, 'signup', 'signup:' || p_user::text)
    on conflict (ref) do nothing;
  return true;
end;
$$;

revoke execute on function public.grant_signup(uuid, text, integer, integer) from public, anon, authenticated;
grant execute on function public.grant_signup(uuid, text, integer, integer) to service_role;
