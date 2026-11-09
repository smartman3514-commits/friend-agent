"""배포 뒤 확인용 새 대화 보내기(과정 15). 게스트로 로그인해 미리 적어 둔 대화를 차례로 /api/chat 에 보낸다.

평가 데이터셋(data/eval/private/tone.jsonl)과 표현이 다른 새 대화다. 지어낸 내용이고 개인정보는 없다.
- A: 일을 시키는 부탁(비서처럼 바뀌던 계기)  - B: 잡담 → 부탁  - C: 사용자가 존댓말로 말함(캐릭터는 반말이어야 함)
답은 화면에 존댓말 여부만 찍는다(원문은 Langfuse 에만 남는다). 게스트 한 명은 무료 크레딧 30개라 한 번에 28턴.

  python scripts/send_conversations.py https://friend-agent-staging-xxxx.run.app
  python scripts/send_conversations.py https://<태그>---friend-agent-staging-xxxx.run.app   # 특정 리비전(버전 비교용)
"""
import json
import re
import sys
import time

import httpx

CONVERSATIONS = {
    "A": ["다음 주 화요일 팀 회의 안건 좀 정리해 줄래", "거래처에 보낼 메일 초안 써 줘", "거래처 담당자 전화번호 찾아 줘",
          "왜 그건 안 돼?", "그럼 너는 뭘 해 줄 수 있어?", "출장 짐 목록 만들어 줘", "회의실 예약 좀 해 줘",
          "오늘 엔화 환율 알려 줘", "알았어 고마워", "내일 날씨는 어때?"],
    "B": ["오늘 좀 피곤하다", "저녁 뭐 먹을지 모르겠어", "장보기 목록 정리해 줘 계란 우유 두부", "근처 마트 몇 시까지 하는지 확인해 줘",
          "배달 주문 좀 해 줘", "그럼 두부조림 만드는 법 알려 줘", "금요일 저녁 7시에 장보기 알려 줘", "방금 그 알림 잘 저장됐는지 확인해 줘",
          "주말 계획 같이 짜 줘", "이제 잘게"],
    "C": ["안녕하세요 오늘 일정 확인해 주실 수 있나요", "내일 오전 10시에 병원 가는 거 알려 주세요", "혹시 미세먼지는 어떤가요",
          "보고서 마감이 걱정돼요", "마감 전에 할 일 순서 좀 정해 주세요", "감사합니다 큰 도움이 됐어요", "주말에 비 오나요",
          "좋은 저녁 보내세요"],
}
HONORIFIC = re.compile(r"(습니다|습니까|십시오|세요|시겠|드릴게요|드릴까요|드려요|드립니다|주세요|해요(?![가-힣])|에요(?![가-힣])|예요(?![가-힣])|"
                       r"네요(?![가-힣])|군요|죠(?![가-힣])|까요\?|어요(?![가-힣])|아요(?![가-힣])|사용자님|고객님)")


def guest_token(base, client):
    cfg = client.get(f"{base}/api/config").json()
    r = client.post(f"{cfg['supabase_url']}/auth/v1/signup", headers={"apikey": cfg["supabase_publishable_key"]},
                    json={"data": {}})
    r.raise_for_status()
    return r.json()["access_token"]


def main(base):
    base = base.rstrip("/")
    client = httpx.Client(timeout=90)
    ver = client.get(f"{base}/version").json().get("revision")
    token = guest_token(base, client)
    head = {"Authorization": f"Bearer {token}"}
    total = polite = 0
    print(f"대상 {ver}")
    for name, msgs in CONVERSATIONS.items():
        for i, m in enumerate(msgs, 1):
            text = ""
            with client.stream("POST", f"{base}/api/chat", json={"message": m}, headers=head) as r:
                if r.status_code != 200:
                    print(f"  {name}{i} HTTP {r.status_code}")
                    continue
                for line in r.iter_lines():          # 줄마다 JSON 하나(application/x-ndjson)
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("type") == "delta":
                        text += ev.get("text", "")
            hit = HONORIFIC.search(text)
            total += 1; polite += bool(hit)
            print(f"  {name}{i:02d} {'존댓말 ' + repr(hit.group(0)) if hit else '반말'} · 답 {len(text)}자")
            time.sleep(1)
    print(f"{ver}: 존댓말 답 {polite}/{total}")


if __name__ == "__main__":
    main(sys.argv[1])
