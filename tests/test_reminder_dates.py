# tests/test_reminder_dates.py
# 10-26 staging에서 발견: 지난 일정이 적힌 사진을 보내면 모델이 날짜를 오늘로 옮겨 저장하고,
# 답은 원래 날짜로 저장했다고 말하고, 세 번째 일정은 말없이 빠졌다.
# 모델 대신 정해진 도구 선택을 돌려주는 가짜 모델로, 코드 쪽 안전장치가 막는지 확인한다.
import json
from datetime import timedelta

from fastapi.testclient import TestClient

from app import when
from app.llm import FakeLLM
from app.main import app, get_llm
from app.router import MAX_REMINDERS

client = TestClient(app)


class ModelLLM(FakeLLM):
    """도구 선택만 정해진 답을 돌려준다. 답 생성은 FakeLLM 그대로(도구 결과를 그대로 읽어 준다)."""

    can_reason = True

    def __init__(self, tools):
        super().__init__(delay=0)
        self.tools = tools

    def complete_json(self, system, user):
        if "도구를 고르는" in system:
            return {"tools": self.tools, "confidence": 0.9, "reason": "약속 부탁"}
        return {}  # 기억 정리는 하지 않는다


def chat_with(tools, message):
    app.dependency_overrides[get_llm] = lambda: ModelLLM(tools)
    try:
        r = client.post("/api/chat", json={"message": message})
    finally:
        app.dependency_overrides.pop(get_llm, None)
    events = [json.loads(line) for line in r.text.splitlines()]
    return events, "".join(e["text"] for e in events if e["type"] == "delta")


def md(d):
    return f"{d.month}월 {d.day}일"


def reminder(text, dt):
    return {"name": "create_reminder", "args": {"text": text, "due_at": dt.replace(second=0, microsecond=0).isoformat()}}


def saved_texts():
    return [r["text"] for r in client.get("/api/reminders").json()]


def test_past_date_is_not_moved_to_another_day():
    now = when.now_in("Asia/Seoul")
    past = now - timedelta(days=5)
    tomorrow = (now + timedelta(days=1)).replace(hour=19, minute=0)
    # 모델이 5일 전 일정을 내일 19:00으로 옮겨 넘긴 상황
    events, reply = chat_with([reminder("지난 정기 모임", tomorrow)], f"{md(past)} 19:00 정기 모임 알려 줘")
    tool = next(e for e in events if e["type"] == "tool")
    assert tool["ok"] is False
    assert "지난 정기 모임" not in saved_texts()
    assert "저장하지 않았습니다" in reply and "지났습니다" in reply


def test_blocking_several_past_reminders_reports_the_guard_once():
    now = when.now_in("Asia/Seoul")
    pasts = [now - timedelta(days=d) for d in (7, 5, 3)]
    tomorrow = (now + timedelta(days=1)).replace(hour=19, minute=0)
    # 사진 속 지난 일정 여러 개를 모델이 전부 내일로 옮겨 넘긴 상황
    tools = [reminder(f"지난 일정{i}", tomorrow + timedelta(minutes=i)) for i in range(1, 4)]
    message = " / ".join(f"{md(d)} 19:00 일정{i}" for i, d in enumerate(pasts, 1)) + " 알려 줘"
    events, _ = chat_with(tools, message)
    guards = next(e for e in events if e["type"] == "turn")["guards"]
    blocked = [g for g in guards if "모두 지났는데" in g]
    assert len(blocked) == 1
    assert len(guards) == len(set(guards))
    assert all(e["ok"] is False for e in events if e["type"] == "tool")


def schedule(n, tag):
    now = when.now_in("Asia/Seoul")
    days = [(now + timedelta(days=d)).replace(hour=10, minute=0) for d in range(2, 2 + n)]
    message = " / ".join(f"{md(d)} 10:00 일정{i}" for i, d in enumerate(days, 1)) + " 다 알려 줘"
    return [reminder(f"{tag} 일정{i}", d) for i, d in enumerate(days, 1)], message


def test_three_reminders_from_a_schedule_are_all_saved():
    tools, message = schedule(3, "셋 중")
    _, reply = chat_with(tools, message)
    saved = saved_texts()
    assert all(f"셋 중 일정{i}" in saved for i in (1, 2, 3))
    assert "이번에 실제로 저장된 약속: '셋 중 일정1', '셋 중 일정2', '셋 중 일정3'" in reply


def test_same_reminder_picked_twice_is_saved_once():
    tools, message = schedule(2, "두 번")
    _, reply = chat_with(tools + tools, message)
    assert saved_texts().count("두 번 일정1") == 1


def test_sending_the_same_schedule_again_does_not_duplicate():
    tools, message = schedule(2, "다시 보냄")
    chat_with(tools, message)
    _, reply = chat_with(tools, message)
    assert saved_texts().count("다시 보냄 일정1") == 1
    assert "이미 저장된 약속입니다" in reply


def test_reminder_not_in_the_message_is_not_saved():
    tools, message = schedule(2, "지어냄")
    extra = reminder("우산 챙기기", when.now_in("Asia/Seoul") + timedelta(days=3))
    events, _ = chat_with(tools + [extra], message)
    turn = next(e for e in events if e["type"] == "turn")
    assert [t["args"]["text"] for t in turn["tools"]] == ["지어냄 일정1", "지어냄 일정2"]


def test_reminder_over_the_limit_is_reported_not_silently_dropped():
    tools, message = schedule(MAX_REMINDERS + 1, "여섯 중")
    _, reply = chat_with(tools, message)
    saved = saved_texts()
    last = f"여섯 중 일정{MAX_REMINDERS + 1}"
    assert f"여섯 중 일정{MAX_REMINDERS}" in saved and last not in saved
    assert "처리하지 않았습니다" in reply and last in reply


def test_day_before_reminder_for_future_date_is_allowed():
    now = when.now_in("Asia/Seoul")
    event = now + timedelta(days=5)
    day_before = (event - timedelta(days=1)).replace(hour=9, minute=0)
    events, _ = chat_with([reminder("모임 하루 전 준비", day_before)], f"{md(event)} 모임 하루 전에 알려 줘")
    assert next(e for e in events if e["type"] == "tool")["ok"] is True
    assert "모임 하루 전 준비" in saved_texts()
