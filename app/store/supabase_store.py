"""Supabase(Postgres)에 저장하는 저장소(STORAGE=supabase).

서버가 Supabase Data API(PostgREST)를 secret key로 부른다.
- secret key는 RLS를 건너뛰므로, 모든 조회와 변경에 로그인으로 확인한 user_id 조건을 코드가 붙인다.
- 테이블에는 RLS를 켜고 공개 정책을 두지 않는다. 브라우저가 publishable key로 테이블을 직접 읽지 못하게 하기 위해서다.
- secret key는 Secret Manager에 두고 SUPABASE_SECRET_KEY 로 받는다. 코드, 로그, 화면에 남기지 않는다.
테이블 구조는 supabase/migrations/ 의 SQL 파일에 있다.
"""
from datetime import datetime, timezone

import httpx

from app.store.base import DEFAULT_SETTINGS


def parse_time(value):
    return datetime.fromisoformat(value) if value else None


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


class SupabaseStore:
    def __init__(self, url, secret_key):
        # secret key 는 JWT가 아니므로 Authorization 이 아니라 apikey 헤더로만 보낸다
        self.http = httpx.Client(base_url=f"{url}/rest/v1", timeout=10,
                                 headers={"apikey": secret_key, "Content-Type": "application/json"})

    def _get(self, table, **params):
        r = self.http.get(f"/{table}", params=params)
        r.raise_for_status()
        return r.json()

    def _insert(self, table, row, upsert_on=None):
        headers = {"Prefer": "return=representation"}
        params = {}
        if upsert_on:
            headers["Prefer"] += ",resolution=merge-duplicates"
            params["on_conflict"] = upsert_on
        r = self.http.post(f"/{table}", json=row, headers=headers, params=params)
        r.raise_for_status()
        return r.json()[0]

    def _update(self, table, values, **filters):
        r = self.http.patch(f"/{table}", json=values, params=filters, headers={"Prefer": "return=representation"})
        r.raise_for_status()
        return r.json()

    def _delete(self, table, **filters):
        self.http.delete(f"/{table}", params=filters).raise_for_status()

    # 캐릭터와 사용자 설정
    def get_character(self, uid):
        rows = self._get("characters", user_id=f"eq.{uid}", select="data")
        return rows[0]["data"] if rows else None

    def save_character(self, uid, character):
        self._insert("characters", {"user_id": uid, "data": character, "updated_at": iso(datetime.now(timezone.utc))},
                     upsert_on="user_id")

    def get_settings(self, uid):
        rows = self._get("user_settings", user_id=f"eq.{uid}", select="location,timezone,push_enabled")
        return {**DEFAULT_SETTINGS, **(rows[0] if rows else {})}

    def save_settings(self, uid, settings):
        self._insert("user_settings", {"user_id": uid, **self.get_settings(uid), **settings}, upsert_on="user_id")

    # 대화
    def _message(self, row):
        return {"id": row["id"], "role": row["role"], "content": row["content"],
                "meta": row.get("meta") or {}, "created_at": parse_time(row["created_at"])}

    def add_message(self, uid, role, content, meta=None):
        return self._message(self._insert("messages", {"user_id": uid, "role": role, "content": content, "meta": meta or {}}))

    def recent_messages(self, uid, limit):
        rows = self._get("messages", user_id=f"eq.{uid}", order="id.desc", limit=limit)
        return [self._message(r) for r in reversed(rows)]

    def get_message(self, uid, message_id):
        """이 사용자의 메시지 하나. 다른 사람의 메시지면 None(user_id 조건을 함께 건다)."""
        rows = self._get("messages", user_id=f"eq.{uid}", id=f"eq.{message_id}", limit=1)
        return self._message(rows[0]) if rows else None

    def messages_after(self, uid, after_id):
        rows = self._get("messages", user_id=f"eq.{uid}", id=f"gt.{after_id or 0}", order="id.asc")
        return [self._message(r) for r in rows]

    def get_summary(self, uid):
        rows = self._get("summaries", user_id=f"eq.{uid}", select="text,covers_until")
        return rows[0] if rows else None

    def save_summary(self, uid, text, covers_until):
        self._insert("summaries", {"user_id": uid, "text": text, "covers_until": covers_until}, upsert_on="user_id")

    # 기억 메모
    def _memory(self, row):
        return {"id": row["id"], "kind": row["kind"], "text": row["text"],
                "created_at": parse_time(row["created_at"]), "updated_at": parse_time(row["updated_at"])}

    def list_memories(self, uid):
        return [self._memory(r) for r in self._get("memories", user_id=f"eq.{uid}", order="id.asc")]

    def add_memory(self, uid, kind, text):
        return self._memory(self._insert("memories", {"user_id": uid, "kind": kind, "text": text}))

    def update_memory(self, uid, memory_id, text):
        self._update("memories", {"text": text, "updated_at": iso(datetime.now(timezone.utc))},
                     user_id=f"eq.{uid}", id=f"eq.{memory_id}")

    def delete_memory(self, uid, memory_id):
        self._delete("memories", user_id=f"eq.{uid}", id=f"eq.{memory_id}")

    def clear_memories(self, uid):
        self._delete("memories", user_id=f"eq.{uid}")

    # 약속
    def _reminder(self, row):
        return {"id": row["id"], "text": row["text"], "due_at": parse_time(row["due_at"]), "status": row["status"],
                "delivered_at": parse_time(row.get("delivered_at")), "created_at": parse_time(row["created_at"])}

    def add_reminder(self, uid, text, due_at):
        return self._reminder(self._insert("reminders", {"user_id": uid, "text": text, "due_at": iso(due_at)}))

    def list_reminders(self, uid, status=None):
        filters = {"user_id": f"eq.{uid}", "order": "due_at.asc"}
        if status:
            filters["status"] = f"eq.{status}"
        return [self._reminder(r) for r in self._get("reminders", **filters)]

    def cancel_reminder(self, uid, reminder_id):
        self._update("reminders", {"status": "cancelled"}, user_id=f"eq.{uid}", id=f"eq.{reminder_id}", status="eq.pending")

    def reschedule_reminder(self, uid, reminder_id, due_at):
        """대기 중인 약속의 시각만 바꾼다(새로 만들지 않는다). 바꿨으면 그 약속, 아니면 None."""
        rows = self._update("reminders", {"due_at": iso(due_at)}, user_id=f"eq.{uid}", id=f"eq.{reminder_id}", status="eq.pending")
        return self._reminder(rows[0]) if rows else None

    def claim_due_reminders(self, uid, now):
        """status 가 pending 인 행만 바꾸는 조건부 업데이트. 요청 두 개가 동시에 와도 한쪽만 그 행을 돌려받는다."""
        rows = self._update("reminders", {"status": "delivered", "delivered_at": iso(datetime.now(timezone.utc))},
                            user_id=f"eq.{uid}", status="eq.pending", due_at=f"lte.{iso(now)}")
        return [self._reminder(r) for r in rows]

    def delete_all(self, uid):
        # credit_ledger, orders 는 돈의 기록이라 지우지 않는다(지우면 가입 무료 크레딧을 다시 받을 수도 있다)
        for table in ("messages", "summaries", "memories", "reminders", "characters", "user_settings"):
            self._delete(table, user_id=f"eq.{uid}")

    # 크레딧 원장 (supabase/migrations/0002_credits.sql)
    def _rpc(self, name, **args):
        r = self.http.post(f"/rpc/{name}", json=args)
        r.raise_for_status()
        return r.json()

    def add_credit(self, uid, delta, reason, ref):
        """새 줄을 쌓으면 True, 같은 ref 가 이미 있으면 False(ref 유일 제약이 409 로 거절)."""
        r = self.http.post("/credit_ledger", json={"user_id": uid, "delta": delta, "reason": reason, "ref": ref},
                           headers={"Prefer": "return=minimal"})
        if r.status_code == 409:
            return False
        r.raise_for_status()
        return True

    def grant_signup(self, uid, ip_hash, amount, limit):
        """가입 무료 크레딧. IP 별 잠금을 거는 DB 함수가 하루 limit 명까지만 준다. 받았으면 True."""
        return bool(self._rpc("grant_signup", p_user=uid, p_ip_hash=ip_hash, p_amount=amount, p_limit=limit))

    def credit_balance(self, uid):
        return int(self._rpc("credit_balance", p_user=uid))

    def spend_credit(self, uid, amount, reason, ref):
        """사용자마다 잠금을 거는 DB 함수로 확인과 빼기를 한 번에 한다. (통과 여부, 남은 잔액)"""
        r = self.http.post("/rpc/spend_credit", json={"p_user": uid, "p_amount": amount, "p_reason": reason, "p_ref": ref})
        if r.status_code == 409:  # 같은 ref 가 이미 있다(같은 일을 두 번 빼지 않는다)
            return False, self.credit_balance(uid)
        r.raise_for_status()
        row = r.json()[0]
        return bool(row["ok"]), int(row["balance"])

    def credit_history(self, uid, limit):
        rows = self._get("credit_ledger", user_id=f"eq.{uid}", order="id.desc", limit=limit,
                         select="id,delta,reason,ref,created_at")
        return [{**r, "created_at": parse_time(r["created_at"])} for r in rows]

    # 결제 주문
    def _order(self, row):
        return {**row, "created_at": parse_time(row["created_at"]), "approved_at": parse_time(row.get("approved_at")),
                "refunded_at": parse_time(row.get("refunded_at"))}

    def create_order(self, uid, order):
        return self._order(self._insert("orders", {**order, "user_id": uid}))

    def get_order(self, uid, order_id):
        rows = self._get("orders", user_id=f"eq.{uid}", order_id=f"eq.{order_id}", limit=1)
        return self._order(rows[0]) if rows else None

    def complete_order(self, uid, order_id, payment_key, approved_at):
        """status 가 pending 인 주문만 바꾸는 조건부 업데이트. 바꿨으면 True."""
        rows = self._update("orders", {"status": "done", "payment_key": payment_key,
                                       "approved_at": approved_at.isoformat() if approved_at else None},
                            user_id=f"eq.{uid}", order_id=f"eq.{order_id}", status="eq.pending")
        return bool(rows)

    def move_order(self, uid, order_id, expected, new, **fields):
        """status 가 expected 인 주문만 바꾸는 조건부 업데이트(환불 중복 막기). 바꿨으면 True."""
        values = {"status": new, **{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in fields.items()}}
        rows = self._update("orders", values, user_id=f"eq.{uid}", order_id=f"eq.{order_id}", status=f"eq.{expected}")
        return bool(rows)

    def list_orders(self, uid, limit):
        return [self._order(r) for r in self._get("orders", user_id=f"eq.{uid}", order="created_at.desc", limit=limit)]

    def fail_order(self, uid, order_id, code):
        rows = self._update("orders", {"status": "failed", "fail_code": code},
                            user_id=f"eq.{uid}", order_id=f"eq.{order_id}", status="eq.pending")
        return bool(rows)
