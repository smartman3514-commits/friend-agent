-- 크레딧 원장에 사진 글자 읽기 사유('ocr')를 더한다.
-- Supabase 대시보드의 SQL Editor에 붙여 넣어 실행한다(0002_credits.sql 다음에). staging, production 에 각각 한 번씩.
-- 이 SQL 을 먼저 실행하고 나서 코드를 배포한다. 반대로 하면 사진 읽기가 제약에 걸려 실패한다.
--
-- 그동안 사진 읽기는 reason='chat', ref='ocr:요청ID' 로 쌓였다. 원장은 고치지 않으므로 지난 줄은 그대로 두고,
-- 사진 읽기를 셀 때는 ref 가 'ocr:' 로 시작하는 줄도 함께 센다.

alter table public.credit_ledger drop constraint if exists credit_ledger_reason_check;
alter table public.credit_ledger add constraint credit_ledger_reason_check
  check (reason in ('signup', 'chat', 'ocr', 'refund', 'purchase'));

-- 확인: 아래 결과에 'ocr' 이 보여야 한다
select conname, pg_get_constraintdef(oid) from pg_constraint
where conrelid = 'public.credit_ledger'::regclass and contype = 'c' and conname like '%reason%';
