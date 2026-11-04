"""크레딧 규칙과 상품표. 숫자는 모두 여기 한 곳에만 둔다.

- 메시지 하나에 크레딧 1개. 메시지를 받자마자 빼고, 답을 만들지 못하면 되돌리는 줄(+1)을 쌓는다.
  받자마자 빼야 잔액 1에서 요청을 동시에 여러 개 보내도 하나만 통과한다.
- 처음 온 사용자(게스트 포함)에게 무료 크레딧 FREE_CREDITS 개. 참조 signup:사용자ID 가 유일해서 한 번만 들어간다.
  단 같은 IP 에서는 하루(한국 날짜) FREE_PER_IP_PER_DAY 명까지만 준다. 시크릿 창으로 게스트를 계속 새로 만들어
  무료 크레딧을 반복해서 받는 공격(2026-11-04 시험에서 3번 모두 통과)을 막기 위해서다.
  IP 는 그대로 저장하지 않고 서버 비밀값으로 섞은 해시(ip_hash)만 쓴다.
- 환불(사용자가 직접): 결제 후 REFUND_DAYS 일 안, 그 묶음의 크레딧을 하나도 안 썼을 때만 전액.
  크레딧은 먼저 받은 것부터 쓴다(가입 무료 → 먼저 산 묶음). 그래서 '안 썼다'는
  지금 잔액 ≥ 그 묶음 크레딧 + 그 뒤에 산 묶음 크레딧(환불된 것은 뺀다) 일 때다.
- 상품표는 서버에만 있다. 화면은 상품 ID만 보내고, 금액과 크레딧 수는 서버가 여기서 찾는다.

근거(2026-11-04, Langfuse 최근 2주, gpt-4o-mini): 메시지 한 건 평균 약 0.69원, 열 번째 이후 약 0.78원.
크레딧 1개 8~10원이면 원가와 결제 수수료(3.74%)를 넉넉히 넘는다.
"""
import hashlib
import hmac
import os
import uuid
from datetime import timedelta

COST = {"chat": 1}
FREE_CREDITS = int(os.environ.get("FREE_CREDITS", "30") or 30)
FREE_PER_IP_PER_DAY = int(os.environ.get("FREE_PER_IP_PER_DAY", "3") or 3)
LOW_BALANCE = 10  # 이 아래면 화면이 "얼마 남지 않았어요" 띠를 보여 준다

PRODUCTS = {
    "credits_100": {"name": "크레딧 100개", "amount": 1000, "credits": 100},
    "credits_550": {"name": "크레딧 550개", "amount": 5000, "credits": 550},
    "credits_1200": {"name": "크레딧 1,200개", "amount": 10000, "credits": 1200},
}

INSUFFICIENT_MESSAGE = "크레딧을 다 썼어요. 충전하면 바로 이어서 이야기할 수 있어요."

_granted = set()  # 이 인스턴스에서 가입 크레딧을 이미 확인한 사용자(매 요청 DB에 쓰지 않으려고)


def ip_hash(ip, secret):
    """IP 를 그대로 저장하지 않으려고 서버 비밀값으로 섞은 해시 앞 32자만 쓴다."""
    return hmac.new((secret or "local-only").encode(), (ip or "unknown").encode(), hashlib.sha256).hexdigest()[:32]


def ensure_signup(store, uid, ip_key="test"):
    """처음 온 사용자에게 무료 크레딧을 한 번만 쌓는다. 같은 IP 는 하루 FREE_PER_IP_PER_DAY 명까지.
    확인과 지급은 저장소가 IP 별 잠금 안에서 한 번에 한다(동시에 여러 게스트를 만들어도 한도를 넘지 않게)."""
    if uid in _granted:
        return
    store.grant_signup(uid, ip_key, FREE_CREDITS, FREE_PER_IP_PER_DAY)
    _granted.add(uid)


def spend_for_chat(store, uid, ip_key="test"):
    """메시지 하나에 크레딧을 뺀다. (통과 여부, 남은 잔액, 되돌릴 때 쓸 참조)"""
    ensure_signup(store, uid, ip_key)
    ref = f"chat:{uuid.uuid4().hex}"
    ok, balance = store.spend_credit(uid, COST["chat"], "chat", ref)
    return ok, balance, ref


def refund(store, uid, ref):
    store.add_credit(uid, COST["chat"], "refund", f"refund:{ref}")


def product_list():
    return [{"id": pid, **p, "unit_price": round(p["amount"] / p["credits"], 1)} for pid, p in PRODUCTS.items()]


REFUND_DAYS = 7


def refund_block(store, uid, order, now):
    """이 주문을 환불할 수 없으면 사용자에게 보여 줄 이유, 할 수 있으면 None."""
    if order["status"] != "done":
        return "환불할 수 있는 결제가 아니에요."
    if not order.get("approved_at") or now - order["approved_at"] > timedelta(days=REFUND_DAYS):
        return f"결제 후 {REFUND_DAYS}일이 지나 환불할 수 없어요."
    lines = sorted(store.credit_history(uid, 1000), key=lambda r: r["id"])
    at = next((i for i, r in enumerate(lines) if r["ref"] == f"order:{order['order_id']}"), None)
    if at is None:
        return "이 결제의 크레딧 기록을 찾지 못했어요. 문의해 주세요."
    later = sum(r["delta"] for r in lines[at + 1:] if r["ref"].startswith(("order:", "refund:order_")))
    if store.credit_balance(uid) < order["credits"] + later:
        return "이 결제로 받은 크레딧을 이미 써서 환불할 수 없어요."
    return None
