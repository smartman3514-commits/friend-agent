"""말투 평가: 캐릭터가 '반말' 인데 존댓말로 답하는지(2026-11 과정 15, 로그에서 찾은 문제 1번).

데이터셋은 실제 트레이스에서 만든다 — 대화 원문이 들어 있어서 저장소에 올리지 않는다(data/eval/private/, .gitignore).
항목마다 그때 '답 만들기' 단계에 실제로 들어간 앞선 대화와 이번 메시지를 그대로 넣고, 지시문만 지금 코드
(app.agent.system_prompt)로 만들어 답 만들기 단계만 다시 돌린다.

  python scripts/eval_tone.py build --export <Langfuse 내려받은 observations.json>   # 데이터셋 만들기(원문, 내 컴퓨터에만)
  python scripts/eval_tone.py run --runs 3                                            # 지금 코드로 평가

통과 조건: 답에 존댓말 표현이 없고 비어 있지 않으며, 모델에게만 붙인 표시가 새어 나오지 않음.
내보낼 기준(결과 보기 전에 고정, 2026-11-09): 문제 항목 90% 이상 · 정상 항목 95% 이상이면서 고치기 전보다 낮지 않음 · 빈 답 0.
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA = ROOT / "data" / "eval" / "private" / "tone.jsonl"
KST = timezone(timedelta(hours=9))

# 존댓말 표현(반말 캐릭터의 답에 있으면 실패)
HONORIFIC = re.compile(r"(습니다|습니까|십시오|세요|시겠|드릴게요|드릴까요|드려요|드립니다|주세요|해요(?![가-힣])|에요(?![가-힣])|예요(?![가-힣])|"
                       r"네요(?![가-힣])|군요|죠(?![가-힣])|까요\?|어요(?![가-힣])|아요(?![가-힣])|사용자님|고객님)")


LEAK = re.compile(r"(이번 답의 말투|이 표시는|도구 결과:)")   # 모델에게만 붙인 표시가 답에 새어 나오면 실패


def is_banmal(text):
    return bool(text.strip()) and not HONORIFIC.search(text) and not LEAK.search(text)


def _text(x):
    if isinstance(x, str):
        return x
    if isinstance(x, list):
        return " ".join(_text(i) for i in x)
    if isinstance(x, dict):
        return _text(x.get("content", ""))
    return "" if x is None else str(x)


def build(export):
    obs = json.loads(Path(export).read_text(encoding="utf-8"))
    by = defaultdict(list)
    for o in obs:
        by[o["traceId"]].append(o)
    turns = []
    for tid, v in by.items():
        root = next((o for o in v if o["name"] == "chat-turn"), None)
        rep = next((o for o in v if o["name"] == "reply"), None)
        if not root or not rep or root.get("environment") not in ("staging", "local", "default"):
            continue
        msgs = rep.get("input")
        msgs = json.loads(msgs) if isinstance(msgs, str) else msgs
        if not isinstance(msgs, list) or len(msgs) < 2:
            continue
        ans = _text(rep.get("output"))
        turns.append({"tid": tid, "ver": root.get("version") or "", "t": root["startTime"],
                      "history": [{"role": m["role"], "content": _text(m.get("content"))} for m in msgs[1:-1]],
                      "user": _text(msgs[-1].get("content")), "old_answer_banmal": is_banmal(ans)})
    turns.sort(key=lambda t: t["t"])
    drift = [t for t in turns if t["ver"].endswith("00089-nok") and not t["old_answer_banmal"]]
    normal = [t for t in turns if t["old_answer_banmal"]]
    pick = lambda xs, n: [xs[round(i * (len(xs) - 1) / (n - 1))] for i in range(n)] if len(xs) >= n else xs
    items = [{"id": f"P{i + 1:02d}", "kind": "문제", **t} for i, t in enumerate(pick(drift, 15))]
    items += [{"id": f"N{i + 1:02d}", "kind": "정상", **t} for i, t in enumerate(pick(normal, 12))]
    DATA.parent.mkdir(parents=True, exist_ok=True)
    DATA.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in items) + "\n", encoding="utf-8")
    print(f"데이터셋 {len(items)}개(문제 {sum(x['kind'] == '문제' for x in items)} · 정상 {sum(x['kind'] == '정상' for x in items)}) → {DATA.relative_to(ROOT)}"
          f" · 후보: 무너진 턴 {len(drift)} · 반말 턴 {len(normal)}")


def run(runs):
    from app.agent import system_prompt
    try:
        from app.agent import speech_note        # 고친 뒤 코드에만 있다
    except ImportError:
        speech_note = None
    from app.config import load_settings
    from app.llm import make_llm
    from app.persona import DEFAULT_CHARACTER
    llm = make_llm(load_settings())
    items = [json.loads(l) for l in DATA.read_text(encoding="utf-8").splitlines() if l.strip()]
    res = defaultdict(lambda: [0, 0]); empty = 0; fails = defaultdict(int)
    for r in range(runs):
        for it in items:
            now = datetime.fromisoformat(it["t"].replace("Z", "+00:00")).astimezone(KST)
            msgs = [{"role": "system", "content": system_prompt(DEFAULT_CHARACTER, now, "서울", [], None)},
                    *it["history"], {"role": "user", "content": speech_note(it["user"], DEFAULT_CHARACTER) if speech_note else it["user"]}]
            ans = llm.complete_text(msgs)
            ok = is_banmal(ans)
            empty += not ans.strip()
            res[it["kind"]][0] += ok; res[it["kind"]][1] += 1
            if not ok:
                fails[it["id"]] += 1
                m = HONORIFIC.search(ans) or LEAK.search(ans)
                print(f"  실패 {it['id']}(run {r + 1}) '{m.group(0) if m else '빈 답'}'")
    for k, (ok, n) in res.items():
        print(f"{k} 항목: {ok}/{n} 통과 ({ok / n * 100:.0f}%)")
    print(f"빈 답 {empty}개 · 실패한 항목: {dict(fails)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build"); b.add_argument("--export", required=True)
    r = sub.add_parser("run"); r.add_argument("--runs", type=int, default=3)
    a = ap.parse_args()
    build(a.export) if a.cmd == "build" else run(a.runs)
