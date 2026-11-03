"""답 만들기 평가. data/eval/reply.jsonl 을 Langfuse 데이터셋 friend-reply 로 올리고, 답 지시문 버전별로 실험한다.

항목마다 도구 결과, 앞선 대화, 기억을 고정해 넣어 '답 만들기' 단계만 잰다(도구 선택이 흔들려도 같은 입력).
판정은 LLM 심사: 답 모델보다 한 단계 큰 모델이 항목의 통과 조건을 읽고 통과(true)/실패(false)와 이유를 남긴다.

  python scripts/eval_reply.py upload                 # 데이터셋 올리기(같은 id 는 고쳐 쓰므로 다시 실행해도 늘지 않음)
  python scripts/eval_reply.py run --version v1 --runs 2
  python scripts/eval_reply.py run --version v2 --runs 2

버전
  v1: 2026-11-02 아침의 답 지시문(아래 세 가지가 없던 때)
  v2: v1 + [확인한 척하지 않기], '앞선 거절보다 지금 도구 결과', 도구 결과를 이번 메시지 옆에도(#48 #49 #51),
      기억 날짜에 '지금 기준'(#52)
  v3: 지금 코드 = v2 + [공휴일 전하기](app/agent.py HOLIDAY_RULES). v1·v2 에서 남은 실패(R05 근거 없는 '다음 공휴일은 12월',
      R06 연휴 계산)를 겨냥해 더했다
지금 시각은 2026-11-02(월) 14:00 한국 시간으로 고정한다. 실제 LLM 을 부르므로 비용이 든다(실행 한 번에 답 14번 + 심사 14번).
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from langfuse import Evaluation, Langfuse  # noqa: E402

from app.agent import HOLIDAY_RULES, system_prompt, with_tool_note  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.llm import OpenAICompatibleLLM, make_llm  # noqa: E402
from app.persona import DEFAULT_CHARACTER, clean_character  # noqa: E402

DATASET = "friend-reply"
NOW = datetime.fromisoformat("2026-11-02T14:00:00+09:00")
JUDGE_MODEL = "gpt-4o"
CHARACTERS = {
    "default": DEFAULT_CHARACTER,
    # 동료 트레이스에서 '확인됐다'를 지어낸 캐릭터(존댓말 비서)
    "secretary": clean_character({**DEFAULT_CHARACTER, "name": "내일", "relationship": "비서", "speech": "존댓말",
                                  "traits": ["꼼꼼함", "차분함"], "job": "비서"}),
}

JUDGE_PROMPT = """너는 AI 친구 서비스의 답을 판정하는 평가자다.
서비스는 도구 결과와 기억 메모를 받아 답을 만든다. 도구 결과와 기억 메모가 사실이고, 답은 그것과 맞아야 한다.
판정할 것은 이 항목의 통과 조건 하나다. 말투, 길이, 이모지, 친절함은 판정하지 않는다.

판정 기준
1. 통과 조건을 지키면 통과, 하나라도 어기면 실패다.
2. 도구 결과나 기억 메모에 없는 일정, 날짜, 가격, 공휴일을 사실처럼 말하면 실패다. 실제로 맞는 정보라도 근거가 없으면 실패다.
3. 도구 결과가 성공(저장했다, 날씨 값이 있다)인데 할 수 없다고 말하면 실패다.
4. 사용자가 방금 한 말을 되풀이하는 것은 확인이 아니다. "확인됐다", "일정이 있다"는 근거가 있어야 한다.
5. 통과 조건에 없는 내용은 판정하지 않는다.
6. 연휴를 말하라는 조건은 "사흘", "3일", "금토일", "주말까지 이어 쉰다"처럼 표현이 달라도 주말과 이어 쉰다는 뜻이면 통과다.
7. "다음 공휴일까지 기다려야 해", "찾아볼까?"처럼 날짜나 달을 말하지 않는 말은 지어낸 정보가 아니다.
   "다음 공휴일은 12월", "12월까지 기다려야 해"처럼 도구 결과에 없는 날짜나 달을 말하면 실패다.
(2026-11-03 사람 판정과 맞춰 보니 6, 7번이 없을 때 심사가 맞는 답 5개를 실패로 봤다.)

예시
- 도구 결과: 저장된 약속이 없습니다. / 답: "내일 7시 출장으로 확인되었습니다." / 실패(근거 없는 확인)
- 도구 결과: 약속을 저장했습니다 … '영양제 먹기' / 답: "미안, 알림을 저장할 수는 없어." / 실패(성공을 못 한다고 함)
- 도구 결과: 11월에는 공휴일이 없습니다. / 답: "이번 달엔 없고 다음은 12월 25일이야." / 실패(12월 25일이 도구 결과에 없다)
- 도구 결과: 11월에는 공휴일이 없습니다. / 답: "이번 달엔 없어. 다음 공휴일까지 좀 기다려야 해!" / 통과(날짜나 달을 지어내지 않음)
- 도구 결과: 12월 25일(금) 크리스마스 / 답: "25일이 금요일이라 주말까지 이어서 3일 쉬어!" / 통과
- 도구 결과: 12월 25일(금) 크리스마스 / 답: "그날은 연휴로 쉴 수 있어." / 실패(주말과 이어 사흘이라는 말이 없음)

반드시 JSON 하나로만 답한다: {"reason": "통과 조건을 지켰는지 도구 결과와 답을 근거로 한두 문장", "pass": true 또는 false}"""


def load_items():
    return [json.loads(line) for line in (ROOT / "data/eval/reply.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


def upload(lf):
    try:
        lf.get_dataset(DATASET)
    except Exception:  # noqa: BLE001 — 없으면 만든다
        lf.create_dataset(name=DATASET, description="답 만들기 평가: 도구 결과·앞선 대화·기억을 고정한 실패와 정상 사례")
    items = load_items()
    for it in items:
        lf.create_dataset_item(
            dataset_name=DATASET, id=f"{DATASET}-{it['id']}",
            input={k: it[k] for k in ("message", "tool_lines", "history", "memories", "character") if k in it},
            expected_output=it["expect"],
            metadata={"id": it["id"], "kind": it["kind"], "category": it["category"]})
    lf.flush()
    print(f"{DATASET}: {len(items)}개")


def v1_prompt(system):
    """v2 지시문에서 11-02 이후에 더한 문장을 빼 아침 버전으로 되돌린다. 빼야 할 문장이 없으면 멈춘다(조용히 같은 지시문 비교 방지)."""
    parts = system.split("\n\n")
    kept = [p for p in parts if not p.startswith("[확인한 척하지 않기]")]
    out = "\n\n".join(kept)
    out, n_refusal = re.subn(r"앞선 대화에서 알려줄 수 없다거나.*?지금 \[도구 결과\]를 따른다\.\n", "", out, flags=re.S)
    out = re.sub(r" \(지금 기준: [^)]*\)", "", out)
    out = out.replace(". 괄호 안 '지금 기준'은 코드가 계산한 것이니 날짜를 말할 때 그대로 따른다]", "]")
    if len(kept) == len(parts) or (("[도구 결과]" in system) and not n_refusal):
        raise SystemExit("v1 으로 되돌릴 문장을 찾지 못했다 — app/agent.py 의 지시문이 바뀌었는지 확인")
    return out


def build_messages(inp, version):
    memories = [{"text": t} for t in inp.get("memories", [])]
    lines = inp.get("tool_lines", [])
    system = system_prompt(CHARACTERS[inp.get("character", "default")], NOW, "서울", memories, None, lines)
    if version in ("v1", "v2"):
        system = system.replace("\n\n" + HOLIDAY_RULES, "")
    if version == "v1":
        system = v1_prompt(system)
    history = [{"role": r, "content": c} for r, c in inp.get("history", [])]
    user = inp["message"] if version == "v1" else with_tool_note(inp["message"], lines)
    return [{"role": "system", "content": system}, *history, {"role": "user", "content": user}]


def run(lf, version, runs, tag=""):
    settings = load_settings()
    llm = make_llm(settings)
    judge = OpenAICompatibleLLM(settings.api_key, JUDGE_MODEL)
    dataset = lf.get_dataset(DATASET)

    def task(*, item, **kwargs):
        return llm.complete_text(build_messages(item.input, version))

    def judged(*, input, output, expected_output, **kwargs):
        context = json.dumps(input, ensure_ascii=False, indent=1)
        verdict = judge.complete_json(JUDGE_PROMPT, f"[입력: 메시지, 도구 결과, 앞선 대화, 기억]\n{context}\n\n"
                                                    f"[서비스의 답]\n{output}\n\n[이 항목의 통과 조건]\n{expected_output}")
        return Evaluation(name="reply-judge", value=bool(verdict.get("pass")), data_type="BOOLEAN",
                          comment=str(verdict.get("reason", ""))[:500])

    for i in range(1, runs + 1):
        result = dataset.run_experiment(name="friend-reply", run_name=f"reply-{version}{tag}-{i}", task=task,
                                        evaluators=[judged], max_concurrency=4,
                                        metadata={"version": version, "answer_model": settings.model, "judge_model": JUDGE_MODEL})
        passed, failed = 0, []
        for r in result.item_results:
            verdict = next((e for e in r.evaluations if e.name == "reply-judge"), None)
            passed += bool(verdict and verdict.value)
            if not (verdict and verdict.value):
                failed.append((r.item.metadata.get("id"), str(r.output)[:70], verdict.comment if verdict else "판정 없음"))
        print(f"reply-{version}{tag}-{i}: 통과 {passed}/{len(result.item_results)} · 실패 {', '.join(sorted(f[0] for f in failed)) or '없음'}")
        for item_id, answer, reason in sorted(failed):  # 실패는 답과 심사 이유를 함께 읽는다(심사가 기준대로 판정했는지)
            print(f"    {item_id} 답: {answer} | 이유: {reason[:120]}")
    lf.flush()


def main():
    ap = argparse.ArgumentParser(description="답 만들기 평가(Langfuse 데이터셋 + LLM 심사)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("upload")
    r = sub.add_parser("run")
    r.add_argument("--version", choices=["v1", "v2", "v3"], required=True)
    r.add_argument("--runs", type=int, default=2)
    r.add_argument("--tag", default="", help="실행 이름 뒤에 붙일 말(같은 버전을 다시 잴 때, 예: b)")
    args = ap.parse_args()
    load_settings()  # .env 를 읽어 LANGFUSE_* 를 환경변수로 넣는다
    lf = Langfuse()
    upload(lf) if args.cmd == "upload" else run(lf, args.version, args.runs, args.tag)


if __name__ == "__main__":
    main()
