"""메시지 하나를 처리하는 흐름(한 턴).

① 안전장치: 위기 표현이면 정해진 안전 응답으로 끝낸다(코드 규칙).
② 도구 선택: 모델이 도구를 0~2개 고른다. 확신이 낮으면 되묻는다(router.py).
③ 도구 실행: 날씨, 미세먼지, 공휴일, 약속(tools.py).
④ 답 생성: 캐릭터 지시문 + 기억 + 도구 결과 + 최근 대화로 답을 스트리밍한다.
⑤ 기억 정리: 답을 다 보낸 뒤 같은 요청 안에서 기억 메모와 대화 요약을 갱신한다(memory.py).
   Cloud Run 기본 설정(요청 기반 과금)에서는 응답이 끝난 뒤 CPU가 거의 주어지지 않아서,
   응답 뒤 백그라운드로 돌리면 기억 정리가 멈추거나 늦어진다. 그래서 응답을 닫기 전에 처리한다.

결과는 이벤트(dict)로 하나씩 내보낸다. show_panel 이 꺼져 있으면(production) 개발용 이벤트는 아예 만들지 않는다.

단계마다 Langfuse 관찰 단위로 남긴다(tracing.py): chat-turn(트레이스) 아래 safety, choose-tools, tool:<이름>,
reply, memory-update, memory-summary. 화면은 이 함수의 결과를 조각마다 다른 스레드에서 이어 받을 수 있어서,
chat-turn 은 객체(turn)로 들고 다니고, 단계를 열 때마다 tracing.child 로 turn 을 되살려 그 아래에 붙인다.
사용자, 세션, 버전 같은 속성(attrs)도 그때마다 함께 걸어 모든 단계에 붙게 한다.
"""
from datetime import datetime, timedelta, timezone

from langfuse import propagate_attributes

from app import memory, tracing, when
from app.logs import log
from app.persona import DEFAULT_CHARACTER, build_persona
from app.router import choose
from app.safety import CRISIS_REPLY, is_crisis
from app.tools import TOOLS, Context, run_tool

GREETING_GAP = timedelta(hours=6)  # 마지막 대화 뒤 이만큼 지나면 앱을 열 때 먼저 말을 건다

# 공휴일 도구 결과가 있을 때만 붙인다. 11-03 friend-reply 실험(scripts/eval_reply.py, 버전마다 2번씩 두 차례):
# 이 두 줄이 없을 때 "12월에 연휴 있어?"(12/25 금)에 사흘 연휴를 말한 답 0/4 → 있을 때 4/4,
# 11월에 공휴일이 없다는 결과에 "다음 공휴일은 12월"처럼 지어낸 답도 사라졌다. 다른 항목은 나빠지지 않았다.
HOLIDAY_RULES = ("[공휴일 전하기]\n"
                 "- 도구 결과의 공휴일이 금요일이면 토요일, 일요일과 이어 사흘, 월요일이면 앞의 토요일, 일요일과 이어 사흘 쉰다고 말한다.\n"
                 "- 도구 결과에 없는 공휴일의 날짜나 달(예: '다음 공휴일은 12월')은 말하지 않는다. 찾은 기간 밖은 모른다고 말한다.")


def system_prompt(character, now, location, memories, summary, tool_lines=(), clarify=False):
    parts = [build_persona(character),
             f"[지금] {when.label(now)} ({now.tzinfo}), 사용자 지역: {location}\n[달력]\n{when.calendar_hint(now)}",
             # 11-02 staging(동료 트레이스): 기억에도 약속에도 없는 출장을 "내일 7시 출장 일정으로 확인되었습니다"라고 답했다.
             "[확인한 척하지 않기]\n사용자의 일정·예약·약속을 '확인했다', '확인되었다', '저장돼 있다'고 말하려면 "
             "아래 도구 결과나 기억 메모에 근거가 있어야 한다. 근거가 없으면 확인할 수 없다고 솔직히 말하고, "
             "원하면 약속으로 저장해 알려 줄 수 있다고 묻는다. 사용자가 방금 말한 내용을 되풀이하는 것은 확인이 아니다."]
    block = memory.memory_block(memories, summary, now)
    if block:
        parts.append(block)
    if tool_lines:
        parts.append("[도구 결과]\n" + "\n".join(f"- {line}" for line in tool_lines))
        # 11-02 staging: 버그로 "알려줄 수 없어"라고 답한 기록이 최근 대화에 쌓이자, 도구 결과를 받고도 같은 거절을 되풀이했다
        parts.append("[도구 결과 전하기]\n앞선 대화에서 알려줄 수 없다거나 저장할 수 없다고 했더라도, 지금 [도구 결과]에 있는 내용은 그대로 전한다. "
                     "이번 질문의 답은 앞선 답이 아니라 지금 [도구 결과]를 따른다.\n약속의 날짜와 시각은 [도구 결과]에 적힌 그대로 말한다. 사용자가 말한 날짜와 다르면 [도구 결과]가 맞다. "
                     "'이번에 실제로 저장된 약속'에 없는 약속은 '저장했어', '저장할게'라고 말하지 않고, 저장한 것을 목록으로 다시 적을 때도 거기 있는 것만 적는다. "
                     "앞선 대화에서 그렇게 말했더라도 지금 [도구 결과]가 맞다. "
                     "저장하지 못한 약속은 그 이유를 짧게 전한다.")
        if any("공휴일" in line for line in tool_lines):
            parts.append(HOLIDAY_RULES)
    if clarify:
        parts.append("[되묻기]\n사용자가 무엇을 원하는지 확실하지 않다. 짧게 되물어서 확인한다. "
                     "이번에는 도구를 쓰지 않았으니 약속을 저장했다거나 저장하겠다고 말하지 않는다.")
    return "\n\n".join(parts)


def with_tool_note(message, tool_lines):
    """이번 메시지 바로 뒤에 도구 결과를 한 번 더 붙인다(모델에게만 보내고 저장하지 않는다).

    11-02 staging: "저장할 수 없어"라고 답한 기록이 최근 대화에 쌓이자, 약속이 실제로 저장됐는데도
    답이 같은 거절을 되풀이했다. 지시문 맨 위의 [도구 결과]보다 가장 최근 메시지 옆에 둔 사실을 더 따른다.
    """
    if not tool_lines:
        return message
    return (f"{message}\n\n[이번 메시지의 도구 결과: 앞선 대화와 달라도 이것이 맞다. 이 표시는 사용자에게 보이지 않으니 "
            f"그대로 옮겨 적지 말고 내용만 캐릭터 말투로 전한다]\n" + "\n".join(f"- {line}" for line in tool_lines))


def run_turn(message, uid, llm, store, show_panel):
    turn = tracing.lf().start_observation(name="chat-turn", as_type="span", input=message)
    try:
        yield from _turn(turn, message, uid, llm, store, show_panel)
    finally:  # 사용자가 중간에 창을 닫아도 트레이스는 닫는다
        turn.end()
        tracing.flush_on_cloud_run()


def _turn(turn, message, uid, llm, store, show_panel):
    model = getattr(llm, "model", None)
    settings = store.get_settings(uid)
    character = store.get_character(uid) or DEFAULT_CHARACTER
    now = when.now_in(settings["timezone"])
    attrs = tracing.turn_attributes(uid, now)
    with tracing.inside(turn, attrs):  # chat-turn 자신에도 속성을 붙인다
        pass
    memories, summary, recent = memory.build_context(store, uid)
    history = [{"role": m["role"], "content": m["content"]} for m in recent]
    store.add_message(uid, "user", message)

    # ① 위기 표현
    with tracing.child(turn, attrs, name="safety", as_type="span", input=message) as step:
        crisis = is_crisis(message)
        step.update(output={"crisis": crisis})
    if crisis:
        guards = ["위기 표현이 있어 캐릭터 설정과 상관없이 정해진 안전 응답을 보냈습니다."]
        if show_panel:
            yield {"type": "turn", "tools": [], "confidence": None, "reason": "", "decided_by": "rule",
                   "action": "safety", "guards": guards}
        yield {"type": "delta", "text": CRISIS_REPLY}
        saved = store.add_message(uid, "assistant", CRISIS_REPLY, {"action": "safety", "guards": guards,
                                                                    "trace_id": tracing.trace_id(turn)})
        log("WARNING", "위기 표현 안전 응답")
        with tracing.inside(turn, {**attrs, "tags": ["safety"]}):
            pass
        turn.update(output=CRISIS_REPLY, metadata={"message_id": saved["id"], "safety": True})
        yield {"type": "done", "message_id": saved["id"]}
        return

    # ② 도구 선택 (모델이 고르면 llm.py 가 입력, 출력, 토큰을 적는다. 규칙이 고르면 토큰은 0)
    with tracing.child(turn, attrs, name="choose-tools", as_type="generation", model=model, input=message) as step:
        choice = choose(message, history, now, settings["location"], llm)
        step.update(metadata={"tools": [t["name"] for t in choice.tools], "confidence": choice.confidence,
                              "decided_by": choice.decided_by, "action": choice.action, "guards": choice.guards})
        if choice.decided_by == "rule":
            step.update(output={"tools": choice.tools, "reason": choice.reason})
    if choice.tools:  # 고른 도구 이름을 태그로 붙여 도구별로 걸러 볼 수 있게 한다
        attrs = {**attrs, "tags": [t["name"] for t in choice.tools]}
    if show_panel:
        yield {"type": "turn", "tools": [{"name": t["name"], "label": TOOLS[t["name"]]["label"], "args": t["args"]}
                                         for t in choice.tools],
               "confidence": choice.confidence, "reason": choice.reason, "decided_by": choice.decided_by,
               "action": choice.action, "guards": choice.guards}

    # ③ 도구 실행
    ctx = Context(store, uid, settings["location"], settings["timezone"])
    results = []
    for t in choice.tools:  # 도구마다 실패하는 이유가 달라서 하나씩 남긴다
        with tracing.child(turn, attrs, name=f"tool:{t['name']}", as_type="span", input=t["args"]) as step:
            r = run_tool(t["name"], t["args"], ctx)
            step.update(output={"summary": r.summary, "data": r.data}, metadata={"ok": r.ok},
                        level="DEFAULT" if r.ok else "WARNING")
        results.append(r)
    for r in results:
        if show_panel:
            yield {"type": "tool", "name": r.name, "label": TOOLS[r.name]["label"], "ok": r.ok,
                   "summary": r.summary, "data": r.data}
        if r.ok and r.name in ("create_reminder", "cancel_reminder"):
            yield {"type": "reminder", "action": r.name, "summary": r.summary}

    # ④ 답 생성
    lines = [r.summary for r in results]
    if choice.dropped:  # 한 번에 쓰는 도구 수를 넘어 버린 것도 알려야 "다 저장했어"라고 하지 않는다
        skipped = ", ".join(f"'{t['args'].get('text')}'" if t["args"].get("text") else TOOLS[t["name"]]["label"]
                            for t in choice.dropped)
        lines.append(f"한 번에 처리하는 개수를 넘어 처리하지 않았습니다(저장 안 됨): {skipped}. 다시 말해 주면 처리합니다.")
    tried = [t for t in [*choice.tools, *choice.dropped] if t["name"] == "create_reminder"]
    if tried:  # 모델이 "저장할게"라고 지어내지 않도록, 실제로 저장된 것을 사실 한 줄로 못박는다
        done = [f"'{r.data['text']}'" for r in results if r.ok and r.name == "create_reminder"]
        lines.append(f"이번에 실제로 저장된 약속: {', '.join(done) if done else '없음'}.")
    system = system_prompt(character, now, settings["location"], memories, summary,
                           lines, clarify=choice.action == "clarify")
    if show_panel:
        yield {"type": "context", "memories": [m["text"] for m in memories],
               "summary": summary["text"] if summary else None, "recent_messages": len(history)}
    messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": with_tool_note(message, lines)}]
    with tracing.inside(turn, attrs):  # 답은 조각마다 이어지므로 '지금 열린 관찰 단위'로 두지 않고 객체로 든다
        gen = turn.start_observation(name="reply", as_type="generation", model=model, input=messages)
    reply, usage = "", {}
    try:
        for chunk in llm.chat_stream(messages, usage=usage):
            if not reply:  # 사용자가 기다린 시간은 첫 글자가 나온 시각까지
                gen.update(completion_start_time=datetime.now(timezone.utc))
            reply += chunk
            yield {"type": "delta", "text": chunk}
    finally:
        gen.update(output=reply, model=usage.get("model", model),
                   usage_details={"input": usage.get("input", 0), "output": usage.get("output", 0)})
        gen.end()
    meta = {"tools": [t["name"] for t in choice.tools], "confidence": choice.confidence,
            "action": choice.action, "guards": choice.guards,
            "trace_id": tracing.trace_id(turn)}  # 화면의 좋아요, 별로예요, 신고를 이 트레이스의 점수로 붙인다
    saved = store.add_message(uid, "assistant", reply, meta)

    # ⑤ 기억 정리
    yield {"type": "memory_start"}
    with tracing.child(turn, attrs, name="memory-update", as_type="generation", model=model) as step:
        changes = memory.update_memories(store, uid, message, reply, now, llm)
        saved_texts = {k: [m["text"] for m in v] for k, v in changes.items()}
        step.update(metadata=saved_texts)  # 모델의 답(output)과 실제로 저장된 것(metadata)을 나란히 본다
        if not getattr(llm, "can_reason", False):
            step.update(output=saved_texts)
    turn.update(output=reply, metadata={"message_id": saved["id"], "tools": [t["name"] for t in choice.tools],
                                        "confidence": choice.confidence, "safety": False})
    yield {"type": "memory", "added": [m["text"] for m in changes["added"]],
           "updated": [m["text"] for m in changes["updated"]], "removed": [m["text"] for m in changes["removed"]]}
    yield {"type": "done", "message_id": saved["id"]}
    # 대화 요약은 '끝' 신호를 보낸 뒤에 한다. 화면은 done 을 받으면 바로 다음 메시지를 받는다.
    # (11-02 동료 트레이스: 요약 2.48초가 끼어 그 턴이 8.21초, 그동안 보내기 버튼이 꺼져 있었다)
    # 요청은 아직 열려 있어 Cloud Run 이 CPU 를 그대로 준다(응답을 닫은 뒤 백그라운드로 돌리면 CPU 가 거의 없다).
    with tracing.inside(turn, attrs):
        memory.maybe_summarize(store, uid, llm)


def greet(uid, llm, store):
    """앱을 열 때 먼저 말을 건다. 때가 된 약속이 있거나, 대화가 처음이거나, 오래 쉬었을 때만.

    약속은 '전달됨'으로 바꾸는 데 성공한 것만 전한다. 창을 두 개 열어도 같은 약속을 두 번 전하지 않는다.
    """
    settings = store.get_settings(uid)
    character = store.get_character(uid) or DEFAULT_CHARACTER
    now = when.now_in(settings["timezone"])
    due = store.claim_due_reminders(uid, now)
    last = store.recent_messages(uid, 1)
    if not due and last and now - last[-1]["created_at"] < GREETING_GAP:
        return None

    memories, summary, _ = memory.build_context(store, uid)
    lines = [f"약속 알림: '{r['text']}' ({when.label(r['due_at'].astimezone(now.tzinfo))} 예정이었음)" for r in due]
    system = system_prompt(character, now, settings["location"], memories, summary, lines)
    guide = ["사용자가 방금 앱을 열었다. 네가 먼저 1~2문장으로 말을 건다."]
    guide.append("[도구 결과]의 약속 알림을 꼭 전한다." if due else "전할 약속은 없다. 약속이나 알림 이야기를 꺼내지 않는다.")
    guide.append("기억 메모에 오늘이거나 이미 지난 일정이 있으면 어땠는지 물어본다.")
    if not last:
        guide.append("처음 만난 사이다. 이름을 넣어 짧게 자기소개를 한다.")
    system += "\n\n[먼저 말 걸기]\n" + " ".join(guide)
    # 실제로 말을 걸 때만 트레이스 하나(greeting)를 남긴다. 모델 호출 하나뿐이라 generation 하나로 둔다
    with propagate_attributes(**tracing.turn_attributes(uid, now)),             tracing.lf().start_as_current_observation(name="greeting", as_type="generation", model=getattr(llm, "model", None),
                                                      metadata={"reminders": len(due), "first_meeting": not last}) as obs:
        text = llm.complete_text([{"role": "system", "content": system},
                                  {"role": "user", "content": "(앱을 열었다. 먼저 말을 걸어 줘)"}])
    tracing.flush_on_cloud_run()
    greeting_trace = tracing.trace_id(obs)
    saved = store.add_message(uid, "assistant", text, {"action": "greeting", "reminders": [r["id"] for r in due],
                                                        "trace_id": greeting_trace})
    log("INFO", "먼저 말 걸기", reminders=len(due))
    return {"id": saved["id"], "text": text, "reminders": [r["text"] for r in due]}
