"""크레딧 원장: 서버에서만 빼고, 0이면 402 로 거절하고, 동시에 보내도 잔액보다 많이 쓰지 못한다."""
import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import credits
from app.auth import User
from app.main import app, current_user, store

client = TestClient(app)


@pytest.fixture
def user(monkeypatch):
    """테스트마다 새 사용자. 무료 크레딧은 실제 기본값(30)으로 돌린다."""
    monkeypatch.setattr(credits, "FREE_CREDITS", 30)
    uid = f"credit-test-{uuid.uuid4().hex[:8]}"
    app.dependency_overrides[current_user] = lambda: User(uid, anonymous=True)
    yield uid
    app.dependency_overrides.pop(current_user, None)


def set_balance(uid, target):
    """원장에 줄을 하나 쌓아 잔액을 target 으로 맞춘다(교안: Table Editor 에서 −줄을 직접 넣는 것과 같다)."""
    credits.ensure_signup(store, uid)
    store.add_credit(uid, target - store.credit_balance(uid), "chat", f"test-adjust:{uuid.uuid4().hex}")


def test_new_user_gets_free_credits_once(user):
    # 처음 온 사용자는 무료 30개, 여러 번 열어도 가입 줄은 하나
    assert client.get("/api/credits").json()["balance"] == 30
    credits._granted.discard(user)
    assert client.get("/api/credits").json()["balance"] == 30
    assert [r["reason"] for r in store.credit_history(user, 10)].count("signup") == 1


def test_message_spends_one_credit(user):
    # 메시지 하나를 보내면 잔액이 1 줄고 원장에 −1 줄이 생긴다
    r = client.post("/api/chat", json={"message": "안녕"})
    assert r.status_code == 200
    body = client.get("/api/credits").json()
    assert body["balance"] == 29
    assert body["history"][0]["delta"] == -1 and body["history"][0]["reason"] == "chat"


def test_zero_balance_is_refused_with_402(user):
    # 잔액 0이면 답을 만들지 않고 402 와 정한 문구로 거절한다
    set_balance(user, 0)
    r = client.post("/api/chat", json={"message": "안녕"})
    assert r.status_code == 402
    assert r.json() == {"error": "insufficient_credits", "message": credits.INSUFFICIENT_MESSAGE, "balance": 0}


def test_failed_reply_gives_the_credit_back(user, monkeypatch):
    # 답을 만들다 실패하면 되돌리는 줄(+1)이 쌓여 잔액이 그대로다
    def broken(*a, **k):
        raise RuntimeError("boom")
        yield
    monkeypatch.setattr("app.agent.run_turn", broken)
    r = client.post("/api/chat", json={"message": "안녕"})
    assert any(json.loads(line)["type"] == "error" for line in r.text.splitlines())
    assert client.get("/api/credits").json()["balance"] == 30
    assert [h["reason"] for h in store.credit_history(user, 2)] == ["refund", "chat"]


def test_ten_concurrent_messages_with_one_credit_only_one_passes(user):
    # 더 해보기 3: 잔액 1에서 요청 열 개를 동시에 보내도 하나만 통과한다
    set_balance(user, 1)
    with ThreadPoolExecutor(10) as pool:
        codes = list(pool.map(lambda _: client.post("/api/chat", json={"message": "안녕"}).status_code, range(10)))
    assert sorted(codes) == [200] + [402] * 9
    assert store.credit_balance(user) == 0


def test_delete_my_data_keeps_the_ledger(user):
    # 내 데이터 삭제로 원장이 지워지면 가입 크레딧을 다시 받을 수 있으므로 원장은 남긴다
    client.post("/api/chat", json={"message": "안녕"})
    client.delete("/api/data")
    credits._granted.discard(user)
    assert client.get("/api/credits").json()["balance"] == 29


def test_same_ip_gets_free_credits_for_three_new_users_a_day(monkeypatch):
    # 공격 2: 같은 IP 에서 게스트를 계속 새로 만들어도 무료 크레딧은 하루 3명까지
    monkeypatch.setattr(credits, "FREE_CREDITS", 30)
    monkeypatch.setattr(credits, "FREE_PER_IP_PER_DAY", 3)
    ip = credits.ip_hash(f"203.0.113.{uuid.uuid4().int % 250}", "s")
    guests = [f"guest-{uuid.uuid4().hex[:8]}" for _ in range(5)]
    for g in guests:
        credits.ensure_signup(store, g, ip)
    assert [store.credit_balance(g) for g in guests] == [30, 30, 30, 0, 0]
    # 다른 IP 의 새 사용자는 받고, 이미 받은 사용자는 다시 불러도 그대로
    other = f"guest-{uuid.uuid4().hex[:8]}"
    credits.ensure_signup(store, other, credits.ip_hash("198.51.100.7", "s"))
    credits._granted.discard(guests[0])
    credits.ensure_signup(store, guests[0], ip)
    assert store.credit_balance(other) == 30 and store.credit_balance(guests[0]) == 30


def test_concurrent_new_guests_on_one_ip_do_not_exceed_the_limit(monkeypatch):
    # 같은 IP 로 새 게스트 10명이 동시에 와도 3명만 받는다
    monkeypatch.setattr(credits, "FREE_CREDITS", 30)
    monkeypatch.setattr(credits, "FREE_PER_IP_PER_DAY", 3)
    ip = credits.ip_hash(f"192.0.2.{uuid.uuid4().int % 250}", "s")
    guests = [f"guest-{uuid.uuid4().hex[:8]}" for _ in range(10)]
    with ThreadPoolExecutor(10) as pool:
        list(pool.map(lambda g: credits.ensure_signup(store, g, ip), guests))
    assert sorted(store.credit_balance(g) for g in guests) == [0] * 7 + [30] * 3


def test_ip_is_not_stored_as_is():
    # IP 는 그대로 저장하지 않는다(해시)
    h = credits.ip_hash("203.0.113.5", "secret")
    assert "203.0.113.5" not in h and len(h) == 32 and h != credits.ip_hash("203.0.113.5", "other")
