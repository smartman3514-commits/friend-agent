# 11-02 staging 트레이스에서 발견: 모델이 도구 선택 JSON 을 약속과 다른 모양으로 돌려줄 때 코드가 막는지 본다.
# - 도구를 2개 고르면 confidence, reason 을 tools 목록 안 항목으로 넣어, 확신도 0으로 읽혀 도구를 하나도 쓰지 않았다.
# - "다음 공휴일 언제야?"에 기간을 이번 달로 좁혀 두 번 찾고 '없음'을 받았다.
from app import when
from app.router import choose


class Stub:
    can_reason = True

    def __init__(self, data):
        self.data = data

    def complete_json(self, system, user):
        return self.data


def pick(message, data):
    return choose(message, [], when.now_in("Asia/Seoul"), "서울", Stub(data))


def test_confidence_inside_tools_list_is_used():
    data = {"tools": [{"name": "get_weather", "args": {"location": "서울", "date": "2026-11-03"}},
                      {"name": "get_air_quality", "args": {"location": "서울", "date": "2026-11-03"}},
                      {"confidence": 0.9, "reason": "날씨와 미세먼지를 묻는다"}]}
    result = pick("내일 서울 날씨랑 미세먼지 어때?", data)
    assert result.action == "answer"
    assert [t["name"] for t in result.tools] == ["get_weather", "get_air_quality"]
    assert result.confidence == 0.9
    assert any("도구 목록 안에" in g for g in result.guards)


def test_open_holiday_question_searches_the_next_year_once():
    data = {"tools": [{"name": "get_holidays", "args": {"from": "2026-11-02", "to": "2026-11-30"}},
                      {"name": "get_holidays", "args": {"from": "2026-11-01", "to": "2026-11-30"}}],
            "confidence": 0.9, "reason": "공휴일"}
    result = pick("다음 공휴일 언제야?", data)
    assert result.tools == [{"name": "get_holidays", "args": {}}]


def test_holiday_question_with_a_month_keeps_the_range():
    data = {"tools": [{"name": "get_holidays", "args": {"from": "2026-12-01", "to": "2026-12-31"}}],
            "confidence": 0.9, "reason": "공휴일"}
    result = pick("12월에 공휴일 있어?", data)
    assert result.tools[0]["args"] == {"from": "2026-12-01", "to": "2026-12-31"}


# 11-02 staging(동료 트레이스): "스케쥴 확인해줘"에 도구를 안 쓰고 "주말에 일정이 뭐있지"에 공휴일 도구를 골라,
# 답이 저장된 적 없는 출장을 "확인되었습니다"라고 지어냈다.
def test_schedule_check_looks_at_saved_reminders():
    result = pick("내일 출장준비 됐나 스케쥴 확인해줘", {"tools": [], "confidence": 1, "reason": "일상 대화"})
    assert [t["name"] for t in result.tools] == ["list_reminders"]
    result = pick("주말에 일정이 뭐있지", {"tools": [{"name": "get_holidays", "args": {}}], "confidence": 0.9, "reason": "주말"})
    assert [t["name"] for t in result.tools] == ["list_reminders"]


def test_follow_up_check_after_a_schedule_question():
    history = [{"role": "user", "content": "내일 출장 스케쥴 확인해줘"}, {"role": "assistant", "content": "저장된 약속이 없어요"}]
    result = choose("아침인지 저녁인지 몰라도 7시야 확인해줘", history, when.now_in("Asia/Seoul"), "서울",
                    Stub({"tools": [], "confidence": 1, "reason": "확인 요청"}))
    assert [t["name"] for t in result.tools] == ["list_reminders"]
    # 일정 이야기가 아니면 손대지 않는다
    assert pick("일정조정이 필요해", {"tools": [], "confidence": 0.9, "reason": "대화"}).tools == []
