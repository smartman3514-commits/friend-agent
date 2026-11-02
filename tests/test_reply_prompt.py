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
