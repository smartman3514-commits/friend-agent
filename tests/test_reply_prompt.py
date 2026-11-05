# 11-02 staging 트레이스에서 발견: 버그로 "알려줄 수 없어"라고 답한 기록이 최근 대화에 쌓이자,
# 도구가 결과를 돌려줘도 답이 같은 거절을 되풀이했다. 답 지시문이 지금 도구 결과를 따르라고 말하는지 본다.
from app import when
from app.agent import system_prompt
from app.persona import DEFAULT_CHARACTER


def test_tool_results_override_earlier_refusals():
    now = when.now_in("Asia/Seoul")
    prompt = system_prompt(DEFAULT_CHARACTER, now, "서울", [], None, ["부산 모레 날씨: 구름 조금"])
    assert "앞선 대화에서 알려줄 수 없다거나 저장할 수 없다고 했더라도" in prompt


def test_no_tool_results_no_override_line():
    now = when.now_in("Asia/Seoul")
    assert "앞선 대화에서 알려줄 수 없다" not in system_prompt(DEFAULT_CHARACTER, now, "서울", [], None)


def test_tool_results_are_repeated_next_to_the_message():
    from app.agent import with_tool_note
    assert with_tool_note("안녕", []) == "안녕"
    note = with_tool_note("물 마시라고 알려 줘", ["약속을 저장했습니다: 11월 3일(화) 08:00에 '물 마시기'."])
    assert note.startswith("물 마시라고 알려 줘\n\n[이번 메시지의 도구 결과")
    assert "'물 마시기'" in note


def test_holiday_rules_only_with_holiday_results():
    from app.agent import HOLIDAY_RULES
    now = when.now_in("Asia/Seoul")
    assert HOLIDAY_RULES in system_prompt(DEFAULT_CHARACTER, now, "서울", [], None, ["12월 25일(금) 크리스마스 공휴일"])
    assert HOLIDAY_RULES not in system_prompt(DEFAULT_CHARACTER, now, "서울", [], None, ["서울 내일 날씨: 맑음"])


def test_where_am_i_tells_what_we_know():
    # 11-05 앱: "내가 어디에 있어?"에 모른다고만 했다 → 설정된 지역과 📍 안내를 지시문에 넣는다
    now = when.now_in("Asia/Seoul")
    prompt = system_prompt(DEFAULT_CHARACTER, now, "성남", [], None)
    assert "[내 위치 질문]" in prompt and "'성남'" in prompt and "📍" in prompt
    assert "[도구 결과]" not in prompt   # 가짜 LLM 이 이 글자를 찾아 답을 만들므로 쓰지 않는다


def test_location_question_gets_a_fact_note():
    # 11-05 앱: 위치를 묻는 말에만 설정 지역·모름·📍 사실을 메시지 옆에 붙인다(실제 모델 10번 중 9번 통과, 전 0/10)
    from app.agent import location_note
    for q in ("내가 어디에 있어?", "내가 어디있니?", "나 지금 어디야?", "내 위치 알아?", "여기 어디야?"):
        note = location_note(q, "성남")
        assert note and "성남" in note[0] and any("📍" in line for line in note)
    for q in ("어디 가고 싶어?", "내가 어디 가면 좋을까?", "오늘 날씨 어때?"):
        assert location_note(q, "성남") == []
