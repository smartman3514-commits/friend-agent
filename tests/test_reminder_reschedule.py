# 11-05 staging(앱 대화): "9시 말고 7시에 알려줘요"에 새 약속을 하나 더 만들고(09:00·19:00 두 개),
# "저녁 7시 말고 아침7시요"에는 약속 도구가 돌지 않았는데 답이 "아침 7시로 변경했어!"라고 했다.
# 이제 바꾸는 말은 저장된 약속의 시각을 바꾸고(update_reminder), 날짜 말이 없으면 날짜는 그대로 둔다.
import json
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import when
from app.auth import User
from app.main import app, current_user, store
from app.router import choose
from tests.test_router_model_shape import Stub

client = TestClient(app)
SEOUL = "Asia/Seoul"


@pytest.fixture
def user():
    uid = f"resched-{uuid.uuid4().hex[:8]}"
    app.dependency_overrides[current_user] = lambda: User(uid, anonymous=True)
    yield uid
    app.dependency_overrides.pop(current_user, None)


def chat(message):
    r = client.post("/api/chat", json={"message": message})
    assert r.status_code == 200
    return [json.loads(line) for line in r.text.splitlines()]


def pending(uid):
    tz = when.now_in(SEOUL).tzinfo
    return [(r["text"], r["due_at"].astimezone(tz)) for r in store.list_reminders(uid, status="pending")]


def test_change_time_moves_the_same_reminder(user):
    # 같은 약속 하나의 시각만 바뀐다(새로 만들지 않음), 날짜는 그대로
    chat("내일 아침 9시에 아들 생일이라고 알려 줘")
    first = pending(user)
    assert len(first) == 1 and first[0][1].hour == 9
    day = first[0][1].date()

    events = chat("9시 말고 저녁 7시에 알려줘요")
    assert [e["action"] for e in events if e["type"] == "reminder"] == ["update_reminder"]
    assert [(t, d.date(), d.hour) for t, d in pending(user)] == [(first[0][0], day, 19)]

    events = chat("저녁 7시 말고 아침7시요")   # '알려 줘'가 없어도 바꾼다
    assert [e["action"] for e in events if e["type"] == "reminder"] == ["update_reminder"]
    assert [(d.date(), d.hour) for _, d in pending(user)] == [(day, 7)]


def test_change_without_saved_reminder_says_so(user):
    events = chat("알림 9시 말고 7시로 바꿔줘")
    turn = next(e for e in events if e["type"] == "turn")
    assert [t["name"] for t in turn["tools"]] == ["update_reminder"]
    assert not any(e["type"] == "reminder" for e in events)   # 바꾼 게 없으니 약속 메모도 없다
    assert pending(user) == []


def test_model_creating_a_new_reminder_for_a_change_is_turned_into_update():
    now = when.now_in(SEOUL)
    history = [{"role": "user", "content": "토요일이 아들 생일인데 알려줘"},
               {"role": "assistant", "content": "토요일 09:00에 아들 생일 알려 줄게!"}]
    due = (now + timedelta(days=2)).replace(hour=19, minute=0, second=0, microsecond=0).isoformat()
    made = Stub({"tools": [{"name": "create_reminder", "args": {"text": "아들 생일", "due_at": due}}],
                 "confidence": 0.9, "reason": "시각 변경"})
    result = choose("9시 말고 7시에 알려줘요", history, now, "서울", made)
    assert [t["name"] for t in result.tools] == ["update_reminder"]
    assert result.tools[0]["args"]["query"] == "아들 생일" and result.tools[0]["args"]["time_only"] is True

    nothing = Stub({"tools": [], "confidence": 1, "reason": "대화"})
    result = choose("저녁 7시 말고 아침7시요", history, now, "서울", nothing)
    assert [t["name"] for t in result.tools] == ["update_reminder"]
    assert when.parse_when("아침7시", now).hour == 7


def test_change_words_without_reminder_talk_are_left_alone():
    now = when.now_in(SEOUL)
    history = [{"role": "assistant", "content": "커피 한잔 어때?"}]
    result = choose("커피 말고 차 마실래 3시쯤", history, now, "서울", Stub({"tools": [], "confidence": 1, "reason": "대화"}))
    assert result.tools == []
