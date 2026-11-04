"""결제 승인: 실제 토스페이먼츠는 부르지 않고 승인 API 호출(app.payments.confirm)을 가짜로 바꿔 끼운다.

가짜 응답 모양은 토스페이먼츠 Payment 객체(결제 승인 200 응답)의 필드 가운데 서버가 쓰는 것만 같은 이름으로 둔다:
orderId, totalAmount, status("DONE"), approvedAt. 실패는 {"code", "message"} 에러 객체.
https://docs.tosspayments.com/reference#결제-승인
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import credits, payments
from app.auth import User
from app.main import app, current_user, store

client = TestClient(app)


class FakeToss:
    def __init__(self, status=200, error=None):
        self.calls = []
        self.status, self.error = status, error

    def __call__(self, secret_key, payment_key, order_id, amount, idempotency_key):
        self.calls.append({"order_id": order_id, "amount": amount, "idempotency_key": idempotency_key})
        if self.status != 200:
            return self.status, self.error
        return 200, {"paymentKey": payment_key, "orderId": order_id, "totalAmount": amount, "status": "DONE",
                     "method": "카드", "approvedAt": datetime.now(timezone.utc).isoformat()}


@pytest.fixture
def toss(monkeypatch):
    fake = FakeToss()
    monkeypatch.setattr(payments, "confirm", fake)
    return fake


def login(uid):
    app.dependency_overrides[current_user] = lambda: User(uid, anonymous=True)


@pytest.fixture
def user(monkeypatch):
    monkeypatch.setattr(credits, "FREE_CREDITS", 30)
    uid = f"pay-test-{uuid.uuid4().hex[:8]}"
    login(uid)
    yield uid
    app.dependency_overrides.pop(current_user, None)


def order(product_id="credits_100"):
    r = client.post("/api/orders", json={"product_id": product_id})
    assert r.status_code == 200
    return r.json()


def confirm(o, amount=None):
    return client.post("/api/payments/confirm",
                       json={"paymentKey": "tpay_test_key", "orderId": o["orderId"], "amount": amount or o["amount"]})


def purchase_lines(uid):
    return [h for h in store.credit_history(uid, 50) if h["reason"] == "purchase"]


def test_order_uses_server_price_not_client(user):
    # 화면은 상품 ID만 보낸다. 금액·크레딧을 같이 보내도 서버는 상품표 값만 쓴다
    r = client.post("/api/orders", json={"product_id": "credits_1200", "amount": 100, "credits": 99999})
    assert r.json()["amount"] == 10000 and r.json()["credits"] == 1200
    assert client.post("/api/orders", json={"product_id": "free_million"}).status_code == 400


def test_same_confirm_twice_gives_credits_once(user, toss):
    # 같은 승인 요청 두 번(새로고침): 크레딧은 한 번만, 승인 API도 한 번만, 멱등키는 주문 번호
    o = order()
    first, second = confirm(o), confirm(o)
    assert first.status_code == second.status_code == 200
    assert second.json()["already"] is True
    assert len(purchase_lines(user)) == 1 and store.credit_balance(user) == 100  # 결제한 100개만(가입 크레딧은 화면을 열 때 쌓임)
    assert len(toss.calls) == 1 and toss.calls[0]["idempotency_key"] == o["orderId"]
    assert store.get_order(user, o["orderId"])["status"] == "done"


def test_changed_amount_is_refused_without_calling_toss(user, toss):
    # 주소의 amount 를 1000 → 100 으로 바꾼 요청: 400, 승인 API는 한 번도 안 불림, 크레딧 그대로
    o = order()
    r = confirm(o, amount=100)
    assert r.status_code == 400
    assert toss.calls == [] and purchase_lines(user) == []
    assert store.get_order(user, o["orderId"])["status"] == "pending"


def test_someone_elses_order_is_not_found(user, toss):
    # 다른 사용자의 주문 번호로 승인 요청: 404, 승인 API 안 불림, 양쪽 모두 크레딧 그대로
    o = order()
    login("pay-test-intruder")
    r = confirm(o)
    assert r.status_code == 404
    assert toss.calls == [] and purchase_lines("pay-test-intruder") == [] and purchase_lines(user) == []


def test_rejected_payment_marks_order_failed(user, monkeypatch):
    # 승인 실패(카드사 거절 등): 주문은 실패, 원장 그대로
    fake = FakeToss(400, {"code": "REJECT_CARD_PAYMENT", "message": "한도초과 혹은 잔액부족으로 결제에 실패했습니다."})
    monkeypatch.setattr(payments, "confirm", fake)
    o = order()
    r = confirm(o)
    assert r.status_code == 400 and "잔액부족" in r.json()["detail"]
    assert store.get_order(user, o["orderId"])["status"] == "failed" and purchase_lines(user) == []


def test_unknown_result_keeps_order_pending_and_retry_works(user, monkeypatch):
    # 연결이 끊겨 승인됐는지 모를 때: 주문은 대기로 남고, 같은 멱등키로 다시 승인하면 한 번만 지급
    monkeypatch.setattr(payments, "confirm", FakeToss(0, {}))
    o = order()
    assert confirm(o).status_code == 502
    assert store.get_order(user, o["orderId"])["status"] == "pending"
    retry = FakeToss()
    monkeypatch.setattr(payments, "confirm", retry)
    assert confirm(o).status_code == 200
    assert retry.calls[0]["idempotency_key"] == o["orderId"] and len(purchase_lines(user)) == 1


def test_fail_redirect_marks_order_failed_without_confirm(user, toss):
    # failUrl(사용자가 결제창을 닫음): 주문은 실패, 승인 API는 부르지 않는다
    o = order()
    client.post("/api/payments/fail", json={"orderId": o["orderId"], "code": "PAY_PROCESS_CANCELED"})
    assert store.get_order(user, o["orderId"])["status"] == "failed" and toss.calls == []


def test_secret_key_is_not_in_config():
    # 시크릿 키는 화면 설정에 없다. 클라이언트 키만 있다
    body = client.get("/api/config").text
    assert "test_gsk" not in body and "test_gck" in body


def test_access_log_hides_payment_key():
    # 토스가 successUrl 로 돌려보낸 주소가 uvicorn 접속 기록에 남을 때 paymentKey 는 가린다
    import logging
    from app.main import HidePaymentKey
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1:1", "GET", "/?pay=success&orderId=o1&paymentKey=tgen_ABC&amount=1000", "1.1", 200), None)
    HidePaymentKey().filter(record)
    assert "tgen_ABC" not in record.getMessage() and "paymentKey=***" in record.getMessage()



class FakeCancel:
    def __init__(self, status=200, error=None):
        self.calls, self.status, self.error = [], status, error

    def __call__(self, secret_key, payment_key, order_id, reason, idempotency_key):
        self.calls.append({"order_id": order_id, "idempotency_key": idempotency_key})
        if self.status != 200:
            return self.status, self.error
        return 200, {"paymentKey": payment_key, "orderId": order_id, "status": "CANCELED",
                     "cancels": [{"cancelAmount": 1000, "cancelStatus": "DONE"}]}


def bought(uid, product_id="credits_100"):
    credits.ensure_signup(store, uid)
    o = order(product_id)
    assert confirm(o).status_code == 200
    return o


def refund(o):
    return client.post(f"/api/orders/{o['orderId']}/refund")


def test_unused_purchase_is_refunded_once(user, toss, monkeypatch):
    # 안 쓴 결제는 전액 환불: 토스 취소 1번(멱등키 cancel-주문번호), 원장 −100 한 줄. 두 번 눌러도 한 번만
    cancel = FakeCancel()
    monkeypatch.setattr(payments, "cancel", cancel)
    o = bought(user)
    assert store.credit_balance(user) == 130
    first, second = refund(o), refund(o)
    assert first.status_code == 200 and second.json()["already"] is True
    assert store.credit_balance(user) == 30
    assert [h["ref"] for h in store.credit_history(user, 50)].count(f"refund:{o['orderId']}") == 1
    assert len(cancel.calls) == 1 and cancel.calls[0]["idempotency_key"] == f"cancel-{o['orderId']}"
    assert store.get_order(user, o["orderId"])["status"] == "refunded"


def test_free_credits_are_spent_first(user, toss, monkeypatch):
    # 무료 30개 중 몇 개를 써도 산 100개는 그대로라 환불 가능
    monkeypatch.setattr(payments, "cancel", FakeCancel())
    o = bought(user)
    for _ in range(5):
        client.post("/api/chat", json={"message": "안녕"})
    assert refund(o).status_code == 200 and store.credit_balance(user) == 25


def test_used_purchase_is_not_refunded(user, toss, monkeypatch):
    # 산 크레딧까지 쓰면(잔액 < 100) 환불 거절, 토스 취소는 안 불림
    cancel = FakeCancel()
    monkeypatch.setattr(payments, "cancel", cancel)
    o = bought(user)
    store.add_credit(user, -31, "chat", f"test-spend:{uuid.uuid4().hex}")
    r = refund(o)
    assert r.status_code == 400 and "이미 써서" in r.json()["detail"]
    assert cancel.calls == [] and store.credit_balance(user) == 99


def test_old_purchase_is_not_refunded(user, toss, monkeypatch):
    # 결제 후 7일이 지나면 환불 거절
    monkeypatch.setattr(payments, "cancel", FakeCancel())
    o = bought(user)
    store.orders[o["orderId"]]["approved_at"] -= timedelta(days=8)
    assert refund(o).status_code == 400


def test_cancel_rejected_by_toss_gives_credits_back(user, toss, monkeypatch):
    # 토스가 취소를 거절하면 뺀 크레딧을 되돌리고 주문은 완료 상태로
    monkeypatch.setattr(payments, "cancel", FakeCancel(400, {"code": "NOT_CANCELABLE_PAYMENT", "message": "취소할 수 없는 결제입니다."}))
    o = bought(user)
    r = refund(o)
    assert r.status_code == 400 and store.credit_balance(user) == 130
    assert store.get_order(user, o["orderId"])["status"] == "done"


def test_someone_elses_order_cannot_be_refunded(user, toss, monkeypatch):
    cancel = FakeCancel()
    monkeypatch.setattr(payments, "cancel", cancel)
    o = bought(user)
    login("pay-test-intruder2")
    assert refund(o).status_code == 404 and cancel.calls == []


def test_terms_page_uses_the_real_numbers():
    # 약관의 숫자는 코드 값에서 채운다(코드와 약관이 어긋나지 않게)
    page = client.get("/terms").text
    assert "{{" not in page
    assert f"{credits.REFUND_DAYS}일 안" in page and "1,000원" in page and "1,200" in page
    assert f"하루(한국 날짜) {credits.FREE_PER_IP_PER_DAY}명" in page
