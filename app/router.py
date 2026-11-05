"""메시지마다 쓸 도구를 고르는 도구 선택기.

1. 모델이 메시지와 최근 대화를 읽고 도구를 0~2개 고른다. 확신도(0~1)와 이유를 함께 낸다.
2. 안전장치: 확신도가 기준보다 낮으면 도구를 쓰지 않고 되묻는다(코드 규칙).
fake 모드처럼 모델을 쓰지 않을 때나 모델 응답을 읽지 못했을 때는 키워드 규칙이 같은 형식으로 답한다.
"""
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from app import when
from app.logs import log
from app.tools import CITIES, TOOLS

CONFIDENCE_THRESHOLD = 0.6
MAX_TOOLS = 2
MAX_REMINDERS = 5  # 일정표 사진처럼 약속만 여러 개 부탁할 때는 약속을 이만큼까지 한 번에 저장한다


def split_tools(tools):
    """한 번에 쓸 도구와 넘쳐서 쓰지 않을 도구로 나눈다. 약속이 여러 개면 약속만 MAX_REMINDERS 개까지 쓴다."""
    reminders = [t for t in tools if t["name"] == "create_reminder"]
    kept = reminders[:MAX_REMINDERS] if len(reminders) > 1 else tools[:MAX_TOOLS]
    return kept, [t for t in tools if t not in kept]


def invented(text, message):
    """약속 글의 낱말(두 글자 이상)이 메시지에 하나도 없으면 모델이 지어낸 약속으로 본다."""
    words = [w for w in re.findall(r"[가-힣A-Za-z0-9]+", text) if len(w) >= 2]
    return bool(words) and not any(w in message or w[:2] in message for w in words)


KEYWORDS = {
    "get_weather": ["날씨", "비 와", "비와", "비 올", "비올", "우산", "더워", "추워", "덥", "춥", "기온", "눈 와", "눈 올", "뭐 입", "쌀쌀", "따뜻"],
    "get_air_quality": ["미세먼지", "초미세", "공기", "마스크", "환기", "황사"],
    "get_holidays": ["공휴일", "휴일", "연휴", "빨간 날", "빨간날", "쉬는 날"],
}
# "토요일 날씨 알려 줘"는 약속이 아니다. 약속은 "~라고 알려 줘", "잊지 않게", "리마인드"처럼 부탁하는 말이 있어야 한다.
ASK_PATTERN = re.compile(r"(라고|으라고|하라고)\s*(꼭\s*)?(알려|말해)|잊지 않게|리마인드|깨워")
# 모델이 고른 약속을 남길지 볼 때 쓰는 더 넓은 부탁 표현. "11월 14일에 이사 가"처럼 일정 이야기만 하면 약속이 아니다.
# (11-02 staging 트레이스: 이사 이야기만 했는데 모델이 확신도 1로 약속을 저장하고, 다음 메시지에서 또 저장했다)
REQUEST_PATTERN = re.compile(r"알려\s*(줘|주|달)|알림|약속으로\s*저장|잊지|리마인|깨워|챙겨\s*(줘|주)|말해\s*(줘|주)")
# 바로 앞 답에서 캐릭터가 먼저 "알려 줄까?"라고 물었으면, "응"만 해도 부탁으로 본다
OFFER_PATTERN = re.compile(r"알려\s*줄까|알림.*(할까|맞춰|설정)")
# 약속 시각 바꾸기. 11-05 staging: "9시 말고 7시에 알려줘요"는 새 약속을 하나 더 만들었고, "저녁 7시 말고 아침7시요"는
# '알려 줘'가 없어 약속이 빠진 채 답만 "변경했어"라고 했다. 바꾸는 말 + (시각·날짜) + 앞 대화가 약속이면 바꾸기로 본다.
CHANGE_PATTERN = re.compile(r"말고|바꿔|바꾸|변경|옮겨|미뤄|당겨|늦춰|대신")
REMINDER_TALK = re.compile(r"약속|알려|알림|기억할게|잊지|저장|리마인드")
CANCEL_WORDS = ["취소", "지워", "없던 걸로", "안 알려 줘도", "안 알려줘도"]
# 11-02 staging(동료 트레이스): "스케쥴 확인해줘", "주말에 일정이 뭐있지"에 도구를 안 쓰거나 공휴일 도구를 골라,
# 답이 저장된 적 없는 출장을 "확인되었습니다"라고 지어냈다. 일정·스케줄도 약속 목록으로 본다.
LIST_PATTERN = re.compile(r"(약속|알림|리마인드|일정|스케[줄쥴]).*(뭐|목록|있어|있지|있었|보여|확인)")


def router_prompt():
    tools = "\n".join(f"- {name}: {t['desc']}. 인자 {t['args']}" for name, t in TOOLS.items())
    return "\n".join([
        "너는 AI 친구 서비스에서 사용자 메시지에 답하기 전에 필요한 도구를 고르는 선택기다.",
        "쓸 수 있는 도구:",
        tools,
        "",
        "규칙:",
        "- 도구가 필요 없는 일상 대화면 tools 를 빈 배열로 둔다. 대부분의 메시지는 여기에 해당한다.",
        f"- 꼭 필요한 도구만 최대 {MAX_TOOLS}개 고른다. 묻지 않은 정보의 도구는 덧붙이지 않는다(날씨만 물으면 get_weather 하나).",
        f"- 약속을 여러 개 부탁하면 create_reminder 를 약속마다 하나씩, 최대 {MAX_REMINDERS}개까지 고른다. 이때는 다른 도구를 섞지 않는다.",
        "- 날씨나 미세먼지 이야기를 해도 예보를 묻는 게 아니라 경험이나 하소연이면 도구를 쓰지 않는다.",
        "- location 은 메시지에 도시 이름이 있으면 그 도시, 없으면 사용자 지역이다.",
        "- date 는 [달력]을 보고 정한 YYYY-MM-DD 날짜다. 날짜 표현이 없으면 오늘이다.",
        "- create_reminder 는 사용자가 나중에 알려 달라고 분명히 부탁할 때만 고른다. 일정이 있다는 이야기만 하면 고르지 않는다.",
        "- 사용자가 자기 일정·스케줄·약속을 확인하거나 무엇이 있는지 물으면 list_reminders 를 고른다(공휴일을 묻는 게 아니면 get_holidays 가 아니다).",
        "- create_reminder 의 text 는 알려 줄 내용만 짧게 쓴다. 예: '우산 챙기기', '일찍 자기'. 예시 문구나 메시지에 없는 일정을 약속으로 만들지 않는다.",
        "- create_reminder 의 due_at 은 [지금] 시각과 시간대를 기준으로 계산한다. 시각 없이 날짜만 있으면 09:00, 아침 08:00, 점심 12:00, 저녁 19:00, 밤 21:00 으로 한다.",
        "- 이미 저장한 약속의 시각·날짜를 바꿔 달라면('9시 말고 7시로', '아침으로 옮겨 줘') update_reminder 를 고른다. create_reminder 로 새 약속을 만들지 않는다. "
        "query 는 최근 대화의 그 약속 내용, due_at 은 바꿀 시각이다. 오전·오후를 말하지 않으면 원래 약속과 같은 오전·오후로 본다.",
        "- create_reminder 의 날짜는 메시지에 적힌 날짜를 그대로 쓴다. 이미 지난 날짜여도 오늘이나 다른 날로 옮기지 않는다. 지난 시각은 코드가 거절하고 사용자에게 알린다.",
        "- 최근 대화를 보고 '그럼 모레는?' 같은 이어지는 질문의 도구와 인자를 정한다.",
        "- confidence 는 고른 결과가 맞을 확신을 0과 1 사이 숫자로 쓴다.",
        "- reason 은 고른 이유를 한국어 한 문장으로 쓴다.",
        '- 반드시 JSON 하나로만 답한다: {"tools": [{"name": "도구 이름", "args": {인자}}], "confidence": 0과 1 사이 숫자, "reason": "이유"}',
    ])


@dataclass
class ToolChoice:
    tools: list                          # [{"name": ..., "args": {...}}]
    confidence: float
    reason: str
    decided_by: str                      # model, rule
    action: str = "answer"               # answer, clarify
    guards: list = field(default_factory=list)
    dropped: list = field(default_factory=list)  # 한 번에 쓰는 도구 수(split_tools)를 넘어 쓰지 않은 도구


# ── 키워드 규칙 ─────────────────────────────────────────────────────

def find_city(text):
    return next((c for c in CITIES if c in text), None)


def reminder_text(text):
    """'목요일 아침에 우산 챙기라고 알려 줘' → '우산 챙기기'처럼 알려 줄 내용만 남긴다."""
    t = re.sub(r"(다음\s*주\s*)?[월화수목금토일]요일|오늘|내일|모레|글피|새벽|아침|오전|점심|오후|저녁|밤", " ", text)
    t = re.sub(r"\d+\s*(분|시간)\s*(뒤|후|있다가)|\d{1,2}\s*시(\s*\d{1,2}\s*분|\s*반)?", " ", t)
    t = re.sub(r"\s*(꼭\s*)?(알려\s*줘|알려\s*주라|알려\s*줄래|리마인드\s*해\s*줘|깨워\s*줘|말해\s*줘)[.!?~ ]*$", "", t)
    t = re.sub(r"^[\s,]*(에는|에)\s+", "", t.strip())
    t = re.sub(r"(으라고|라고)$", "기", t.strip())   # 챙기라고 → 챙기기, 먹으라고 → 먹기
    t = re.sub(r"(다고|하고)$", "", t)
    return re.sub(r"\s+", " ", t).strip(" ,.") or text.strip()


def explicit_dates(message, now):
    """메시지에 적힌 'M월 D일'을 날짜로 읽는다. 반년 넘게 지난 날짜는 내년 날짜로 본다(12월에 말한 '1월 5일')."""
    found = []
    for m in re.finditer(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", message):
        try:
            d = date(now.year, int(m.group(1)), int(m.group(2)))
            if (now.date() - d).days > 180:
                d = date(now.year + 1, d.month, d.day)
        except ValueError:
            continue
        found.append(d)
    return found


def spoken_date(text, message, now):
    """약속 글(text)이 나온 메시지 조각의 '내일', '모레', '목요일', '다음 주 토요일'을 날짜로 바꾼다. (표현, 날짜) 또는 None.

    약속을 여러 개 부탁하면 쉼표나 '그리고'로 조각을 나눠 약속 글의 낱말이 든 조각에서만 찾는다.
    'M월 D일'이 적혀 있거나, 한 조각에 날짜 표현이 둘 이상이거나("오늘 말고 내일"), 요일만 말했는데 그날이 오늘이면
    어느 날인지 코드가 단정할 수 없어 None.
    """
    if explicit_dates(message, now):
        return None
    parts = [p for p in re.split(r",|\s그리고\s", message) if p.strip()]
    if len(parts) > 1:
        words = [w for w in re.findall(r"[가-힣A-Za-z0-9]+", text) if len(w) >= 2]
        parts = [p for p in parts if any(w in p or w[:2] in p for w in words)]
    if len(parts) != 1:
        return None
    hits = [w for w in when.DAY_WORDS if w in parts[0]] + re.findall(r"(?:다음\s*주\s*)?[월화수목금토일]요일", parts[0])
    if len(hits) != 1:
        return None
    offset = when.day_offset(hits[0], now)
    if offset is None or (offset == 0 and hits[0] not in when.DAY_WORDS):
        return None
    return hits[0], (now + timedelta(days=offset)).date()


def due_date(value):
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def holiday_range(message, now):
    """'12월에 연휴 있어?' → 그 달의 첫날과 마지막 날. 달이 없으면 빈 인자(앞으로 1년)."""
    m = re.search(r"(\d{1,2})\s*월", message)
    if not m or not 1 <= int(m.group(1)) <= 12:
        return {}
    month = int(m.group(1))
    year = now.year if month >= now.month else now.year + 1
    last = (date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)).day
    return {"from": f"{year}-{month:02d}-01", "to": f"{year}-{month:02d}-{last}"}


def has_date_words(message, now):
    """메시지에 날짜 말(오늘·내일·요일·M월 D일)이 있는지. 없으면 약속을 바꿀 때 날짜는 그대로 두고 시각만 바꾼다."""
    return bool(explicit_dates(message, now) or any(w in message for w in when.DAY_WORDS)
                or re.search(r"[월화수목금토일]요일", message))


def change_request(message, history, now):
    """'~말고 ~시', '~로 바꿔'처럼 앞 대화의 약속 시각을 바꾸는 말이면 바꿀 시각, 아니면 None."""
    if not CHANGE_PATTERN.search(message):
        return None
    last_answer = next((m["content"] for m in reversed(history) if m["role"] == "assistant"), "")
    last_user = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
    if not any(REMINDER_TALK.search(t) for t in (message, last_answer, last_user)):
        return None
    tail = re.split(r"말고|대신", message)[-1]  # "저녁 7시 말고 아침 7시요" → 바꿀 시각은 '말고' 뒤
    return when.parse_when(tail, now)


def rule_choose(message, now, location, history=()):
    picked = []
    change_to = change_request(message, history, now)
    if change_to:
        picked.append({"name": "update_reminder", "args": {"query": "", "due_at": change_to.isoformat(),
                                                           "time_only": not has_date_words(message, now)}})
    elif any(w in message for w in CANCEL_WORDS) and re.search(r"약속|알림|리마인드", message):
        picked.append({"name": "cancel_reminder", "args": {"query": reminder_text(message.replace("취소", ""))}})
    elif LIST_PATTERN.search(message):
        picked.append({"name": "list_reminders", "args": {}})
    elif ASK_PATTERN.search(message) and (due := when.parse_when(message, now)):
        picked.append({"name": "create_reminder", "args": {"text": reminder_text(message), "due_at": due.isoformat()}})
    else:
        day = (now + timedelta(days=when.day_offset(message, now) or 0)).date().isoformat()
        for name, words in KEYWORDS.items():
            if any(w in message for w in words):
                args = {"location": find_city(message) or location, "date": day}
                if name == "get_holidays":
                    args = holiday_range(message, now)
                picked.append({"name": name, "args": args})
    picked = picked[:MAX_TOOLS]
    if not picked:
        return ToolChoice([], 0.8, "도구 키워드가 없는 일상 대화입니다.", "rule")
    labels = ", ".join(TOOLS[p["name"]]["label"] for p in picked)
    return ToolChoice(picked, 0.75, f"'{labels}' 관련 표현이 들어 있습니다.", "rule")


# ── 모델 ────────────────────────────────────────────────────────────

def model_choose(message, history, now, location, llm):
    recent = "\n".join(f"{m['role']}: {m['content']}" for m in history[-4:]) or "(없음)"
    user = (f"[지금] {now.isoformat()} ({when.WEEKDAYS[now.weekday()]}요일)\n[달력]\n{when.calendar_hint(now)}\n"
            f"[사용자 지역] {location}\n[최근 대화]\n{recent}\n\n[메시지]\n{message}")
    data = llm.complete_json(router_prompt(), user)
    guards = []
    # 안전장치: 도구를 2개 이상 고를 때 모델이 confidence, reason 을 tools 목록 안 항목으로 넣는 일이 있다.
    # 그대로 두면 확신도가 0으로 읽혀 되묻기로 빠지고 도구를 하나도 쓰지 않는다(11-02 staging 트레이스).
    for t in data.get("tools") or []:
        if isinstance(t, dict) and "name" not in t and "confidence" in t and "confidence" not in data:
            data["confidence"] = t["confidence"]
            data.setdefault("reason", t.get("reason", ""))
            guards.append("모델이 확신도를 도구 목록 안에 적어 꺼내 썼습니다.")
    # "다음 공휴일"처럼 기간을 말하지 않으면 앞으로 1년에서 찾는다. 모델은 이번 달로 좁혀 '없음'을 받곤 했다.
    open_holidays = not re.search(r"\d{1,2}\s*월|이번\s*달|다음\s*달|올해|내년", message)
    tools, seen = [], set()
    for t in data.get("tools") or []:
        if isinstance(t, dict) and t.get("name") in TOOLS:
            args = t.get("args") if isinstance(t.get("args"), dict) else {}
            if t["name"] == "get_holidays" and open_holidays and args:
                args = {}
                guards.append("기간을 말하지 않은 공휴일 질문이라 앞으로 1년에서 찾습니다.")
            key = (t["name"], json.dumps(args, sort_keys=True, ensure_ascii=False))
            if key in seen:  # 모델이 같은 약속을 두 번 고르면 두 번 저장된다
                continue
            seen.add(key)
            tools.append({"name": t["name"], "args": args})
    confidence = max(0.0, min(1.0, float(data.get("confidence", 0))))
    # 안전장치: 약속 시각을 바꾸는 말인데 모델이 새 약속을 만들거나 아무 도구도 안 고르면 바꾸기로 돌린다.
    # 바꾸기는 '알려 줘'가 없어도 된다(아래 부탁 확인은 새 약속에만 건다).
    change_to = change_request(message, history, now)
    if change_to and not any(t["name"] in ("update_reminder", "cancel_reminder") for t in tools):
        made = [t for t in tools if t["name"] == "create_reminder"]
        query = str(made[0]["args"].get("text") or "") if made else ""
        due_at = str(made[0]["args"].get("due_at") or "") if made else change_to.isoformat()
        tools = [t for t in tools if t not in made]
        tools.insert(0, {"name": "update_reminder", "args": {"query": query, "due_at": due_at}})
        confidence = max(confidence, 0.8)
        guards.append("약속 시각을 바꾸는 말이라 새 약속 대신 저장된 약속의 시각을 바꿉니다.")
    for t in tools:
        if t["name"] == "update_reminder" and not has_date_words(message, now):
            t["args"]["time_only"] = True   # 날짜 말이 없으면 원래 약속 날짜는 그대로, 시각만 바꾼다
    # 안전장치: 알려 달라는 부탁이 없는데 고른 약속은 뺀다(프롬프트로만 막으면 모델이 일정 이야기를 약속으로 저장한다)
    last_answer = next((m["content"] for m in reversed(history) if m["role"] == "assistant"), "")
    if not REQUEST_PATTERN.search(message) and not OFFER_PATTERN.search(last_answer):
        unasked = [t for t in tools if t["name"] == "create_reminder"]
        if unasked:
            tools = [t for t in tools if t not in unasked]
            guards.append("알려 달라는 부탁이 없어 약속을 저장하지 않았습니다: "
                          + ", ".join(f"'{t['args'].get('text')}'" for t in unasked))
    # 안전장치: 자기 일정을 확인해 달라는데 약속 목록을 안 보면, 답이 근거 없이 "확인됐다"고 지어낸다.
    # 약속 목록을 넣고, 공휴일 낱말이 없는데 고른 공휴일 도구는 뺀다.
    # 바로 앞 질문이 일정 확인이었고 이번에도 "확인해줘"면 이어지는 확인으로 본다("7시야 확인해줘").
    prev_user = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
    asks_schedule = LIST_PATTERN.search(message) or ("확인" in message and LIST_PATTERN.search(prev_user))
    if asks_schedule and not any(k in message for k in KEYWORDS["get_holidays"]):
        tools = [t for t in tools if t["name"] != "get_holidays"]
    if asks_schedule and not any(t["name"] in ("list_reminders", "create_reminder", "cancel_reminder") for t in tools):
        tools.insert(0, {"name": "list_reminders", "args": {}})
        guards.append("일정 확인 질문이라 저장된 약속 목록을 함께 봅니다.")
    # 안전장치: 약속을 여러 개 고를 때 메시지에 없는 약속(지시문 예시 '우산 챙기기' 등)을 지어내 개수를 채우면 뺀다
    if sum(t["name"] == "create_reminder" for t in tools) > 1:
        made_up = [t for t in tools if t["name"] == "create_reminder" and invented(str(t["args"].get("text") or ""), message)]
        if made_up:
            tools = [t for t in tools if t not in made_up]
            guards.append("메시지에 없는 약속을 지어내서 뺐습니다: " + ", ".join(f"'{t['args'].get('text')}'" for t in made_up))
    kept, dropped = split_tools(tools)
    result = ToolChoice(kept, confidence, str(data.get("reason", "")), "model", guards=guards, dropped=dropped)
    if dropped:
        result.guards.append(f"도구를 {len(tools)}개 골라 {len(kept)}개만 씁니다.")

    # 안전장치: 지난 연도로 적힌 날짜는 올해로 고친다
    for t in result.tools:
        for key in ("date", "due_at", "from", "to"):
            if key in t["args"]:
                t["args"][key], fixed = when.fix_year(t["args"][key], now)
                if fixed:
                    result.guards.append(f"{t['name']} 의 {key} 연도가 지난 연도라 {now.year}년으로 고쳤습니다.")

    # 안전장치: '내일', '목요일', '다음 주 토요일'을 모델이 다른 날로 잡으면 코드가 계산한 날짜로 고친다(시각은 그대로)
    # (11-02 staging 트레이스: '다음 주 토요일'을 이번 주 11/7로, '내일 저녁 9시'를 오늘 11/2로 저장했다)
    for t in result.tools:
        due = due_date(t["args"].get("due_at")) if t["name"] == "create_reminder" else None
        spoken = due and spoken_date(str(t["args"].get("text") or ""), message, now)
        if spoken and due != spoken[1]:
            word, want = spoken
            fixed = datetime.fromisoformat(t["args"]["due_at"]).replace(year=want.year, month=want.month, day=want.day)
            t["args"]["due_at"] = fixed.isoformat()
            result.guards.append(f"'{word}' 날짜를 {when.date_label(due)}로 잡아 {when.date_label(want)}로 고쳤습니다.")

    # 안전장치: 메시지에 적힌 날짜가 모두 지났는데 모델이 다른 날로 옮겨 약속을 잡으면 막는다
    # (지난 일정이 적힌 사진을 보내면 모델이 오늘 같은 시각으로 바꿔 저장하던 문제)
    dates = explicit_dates(message, now)
    if dates and all(d < now.date() for d in dates):
        shown = ", ".join(when.date_label(d) for d in dates[:3])
        for t in result.tools:
            due = due_date(t["args"].get("due_at")) if t["name"] == "create_reminder" else None
            if due and due not in dates:
                t["args"]["rejected"] = f"메시지에 적힌 날짜({shown})가 이미 지났습니다."
                result.guards.append(f"메시지의 날짜({shown})가 모두 지났는데 다른 날로 약속을 잡으려 해 막았습니다.")
    return result


def choose(message, history, now, location, llm):
    if getattr(llm, "can_reason", False):
        try:
            result = model_choose(message, history, now, location, llm)
        except Exception as error:  # 모델 응답이 형식에 맞지 않으면 규칙으로 대신한다
            log("WARNING", "모델 도구 선택 실패, 키워드 규칙으로 대신함", error=type(error).__name__)
            result = rule_choose(message, now, location, history)
            result.guards.append("모델 응답을 읽지 못해 키워드 규칙으로 골랐습니다.")
    else:
        result = rule_choose(message, now, location, history)

    # 안전장치: 확신이 낮으면 도구를 쓰지 않고 되묻는다
    if result.confidence < CONFIDENCE_THRESHOLD:
        result.action = "clarify"
        result.guards.append(f"확신도 {result.confidence:.2f}가 기준 {CONFIDENCE_THRESHOLD} 미만이라 도구를 쓰지 않고 되묻습니다.")
        result.tools = []
        result.dropped = []

    # 약속마다 같은 문구가 붙으면 '이번 턴' 패널에 같은 줄이 여러 번 보이므로, 순서를 지키며 한 번씩만 남긴다
    result.guards = list(dict.fromkeys(result.guards))

    log("INFO", "도구 선택", tools=[t["name"] for t in result.tools], confidence=round(result.confidence, 2),
        decided_by=result.decided_by, action=result.action)
    return result
