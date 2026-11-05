"""서버 메모리에 저장하는 저장소(STORAGE=memory).

노트북에서 바로 실행해 볼 수 있지만, Cloud Run에서는 문제가 생긴다.
- 새 리비전을 배포하거나 인스턴스가 꺼지면 저장한 것이 모두 사라진다.
- 인스턴스가 두 개 이상이면 요청마다 다른 인스턴스로 갈 수 있어 기억이 갈라진다.
그래서 여러 사람이 쓰기 전에는 STORAGE=supabase 로 바꾼다. 두 저장소는 같은 함수 이름을 쓴다.
"""
import itertools
import threading
from datetime import datetime, timezone

from app.store.base import DEFAULT_SETTINGS


def utcnow():
    return datetime.now(timezone.utc)


class MemoryStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.ids = itertools.count(1)
        self.users = {}
        self.ledger, self.refs, self.orders = [], set(), {}  # 크레딧 원장과 주문은 사용자 데이터와 따로 둔다
        self.free_grants = {}  # user_id → {"ip_hash", "day", "granted"}

    def _user(self, uid):
        return self.users.setdefault(uid, {
            "character": None, "settings": dict(DEFAULT_SETTINGS), "messages": [],
            "summary": None, "memories": [], "reminders": []})

    # 캐릭터와 사용자 설정
    def get_character(self, uid):
        return self._user(uid)["character"]

    def save_character(self, uid, character):
        self._user(uid)["character"] = dict(character)

    def get_settings(self, uid):
        return dict(self._user(uid)["settings"])

    def save_settings(self, uid, settings):
        self._user(uid)["settings"].update(settings)

    # 대화
    def add_message(self, uid, role, content, meta=None):
        with self.lock:
            m = {"id": next(self.ids), "role": role, "content": content, "meta": meta or {}, "created_at": utcnow()}
            self._user(uid)["messages"].append(m)
        return m

    def recent_messages(self, uid, limit):
        return list(self._user(uid)["messages"][-limit:])

    def get_message(self, uid, message_id):
        """이 사용자의 메시지 하나. 다른 사람의 메시지면 None."""
        return next((m for m in self._user(uid)["messages"] if m["id"] == message_id), None)

    def messages_after(self, uid, after_id):
        return [m for m in self._user(uid)["messages"] if m["id"] > (after_id or 0)]

    def get_summary(self, uid):
        return self._user(uid)["summary"]

    def save_summary(self, uid, text, covers_until):
        self._user(uid)["summary"] = {"text": text, "covers_until": covers_until}

    # 기억 메모
    def list_memories(self, uid):
        return [dict(m) for m in self._user(uid)["memories"]]

    def add_memory(self, uid, kind, text):
        with self.lock:
            m = {"id": next(self.ids), "kind": kind, "text": text, "created_at": utcnow(), "updated_at": utcnow()}
            self._user(uid)["memories"].append(m)
        return m

    def update_memory(self, uid, memory_id, text):
        for m in self._user(uid)["memories"]:
            if m["id"] == memory_id:
                m.update(text=text, updated_at=utcnow())

    def delete_memory(self, uid, memory_id):
        u = self._user(uid)
        u["memories"] = [m for m in u["memories"] if m["id"] != memory_id]

    def clear_memories(self, uid):
        self._user(uid)["memories"] = []

    # 약속
    def add_reminder(self, uid, text, due_at):
        with self.lock:
            r = {"id": next(self.ids), "text": text, "due_at": due_at.astimezone(timezone.utc),
                 "status": "pending", "delivered_at": None, "created_at": utcnow()}
            self._user(uid)["reminders"].append(r)
        return dict(r)

    def list_reminders(self, uid, status=None):
        items = [dict(r) for r in self._user(uid)["reminders"] if status in (None, r["status"])]
        return sorted(items, key=lambda r: r["due_at"])

    def cancel_reminder(self, uid, reminder_id):
        for r in self._user(uid)["reminders"]:
            if r["id"] == reminder_id and r["status"] == "pending":
                r["status"] = "cancelled"

    def reschedule_reminder(self, uid, reminder_id, due_at):
        """대기 중인 약속의 시각만 바꾼다(새로 만들지 않는다). 바꿨으면 그 약속, 아니면 None."""
        with self.lock:
            for r in self._user(uid)["reminders"]:
                if r["id"] == reminder_id and r["status"] == "pending":
                    r["due_at"] = due_at.astimezone(timezone.utc)
                    return dict(r)
        return None

    def claim_due_reminders(self, uid, now):
        """때가 된 약속을 '전달됨'으로 바꾸고, 이번에 바꾼 것만 돌려준다(같은 약속을 두 번 전하지 않게)."""
        claimed = []
        with self.lock:
            for r in self._user(uid)["reminders"]:
                if r["status"] == "pending" and r["due_at"] <= now:
                    r.update(status="delivered", delivered_at=utcnow())
                    claimed.append(dict(r))
        return claimed

    def delete_all(self, uid):
        # 크레딧 원장과 주문은 돈의 기록이라 지우지 않는다(지우면 가입 무료 크레딧을 다시 받을 수도 있다)
        self.users.pop(uid, None)

    # 크레딧 원장: 쌓기만 한다. 잔액은 합계. ref 는 유일하다
    def add_credit(self, uid, delta, reason, ref):
        """새 줄을 쌓으면 True, 같은 ref 가 이미 있으면 False(두 번 넣지 않는다)."""
        with self.lock:
            ledger = self.ledger
            if ref in self.refs:
                return False
            self.refs.add(ref)
            ledger.append({"id": len(ledger) + 1, "user_id": uid, "delta": delta, "reason": reason, "ref": ref,
                           "created_at": utcnow()})
            return True

    def grant_signup(self, uid, ip_hash, amount, limit):
        """가입 무료 크레딧. 같은 ip_hash 는 하루(한국 날짜) limit 명까지. 받았으면 True."""
        from app.when import now_in
        today = now_in("Asia/Seoul").date()
        with self.lock:
            if uid in self.free_grants:
                return self.free_grants[uid]["granted"]
            used = sum(1 for g in self.free_grants.values() if g["ip_hash"] == ip_hash and g["day"] == today and g["granted"])
            granted = f"signup:{uid}" in self.refs or used < limit
            self.free_grants[uid] = {"ip_hash": ip_hash, "day": today, "granted": granted}
            if granted and f"signup:{uid}" not in self.refs:
                self.refs.add(f"signup:{uid}")
                self.ledger.append({"id": len(self.ledger) + 1, "user_id": uid, "delta": amount, "reason": "signup",
                                    "ref": f"signup:{uid}", "created_at": utcnow()})
            return granted

    def credit_balance(self, uid):
        return sum(r["delta"] for r in self.ledger if r["user_id"] == uid)

    def spend_credit(self, uid, amount, reason, ref):
        """잔액 확인과 빼기를 잠금 안에서 한 번에 한다. (통과 여부, 남은 잔액)"""
        with self.lock:
            ledger = self.ledger
            balance = sum(r["delta"] for r in ledger if r["user_id"] == uid)
            if balance < amount or ref in self.refs:
                return False, balance
            self.refs.add(ref)
            ledger.append({"id": len(ledger) + 1, "user_id": uid, "delta": -amount, "reason": reason, "ref": ref,
                           "created_at": utcnow()})
            return True, balance - amount

    def credit_history(self, uid, limit):
        rows = [dict(r) for r in self.ledger if r["user_id"] == uid]
        return list(reversed(rows))[:limit]

    # 결제 주문
    def create_order(self, uid, order):
        with self.lock:
            self.orders[order["order_id"]] = {**order, "user_id": uid, "status": "pending", "payment_key": None,
                                              "fail_code": None, "approved_at": None, "refunded_at": None,
                                              "created_at": utcnow()}
        return dict(self.orders[order["order_id"]])

    def get_order(self, uid, order_id):
        """이 사용자의 주문 하나. 다른 사람의 주문이면 None."""
        o = self.orders.get(order_id)
        return dict(o) if o and o["user_id"] == uid else None

    def complete_order(self, uid, order_id, payment_key, approved_at):
        """대기 중인 주문만 완료로 바꾼다. 바꿨으면 True."""
        with self.lock:
            o = self.get_order(uid, order_id)
            if not o or o["status"] != "pending":
                return False
            self.orders[order_id].update(status="done", payment_key=payment_key, approved_at=approved_at)
            return True

    def move_order(self, uid, order_id, expected, new, **fields):
        """주문 상태가 expected 일 때만 new 로 바꾼다(환불 중복 막기). 바꿨으면 True."""
        with self.lock:
            o = self.get_order(uid, order_id)
            if not o or o["status"] != expected:
                return False
            self.orders[order_id].update(status=new, **fields)
            return True

    def list_orders(self, uid, limit):
        rows = [dict(o) for o in self.orders.values() if o["user_id"] == uid]
        return sorted(rows, key=lambda o: o["created_at"], reverse=True)[:limit]

    def fail_order(self, uid, order_id, code):
        with self.lock:
            o = self.get_order(uid, order_id)
            if not o or o["status"] != "pending":
                return False
            self.orders[order_id].update(status="failed", fail_code=code)
            return True
