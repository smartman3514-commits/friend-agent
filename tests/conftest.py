"""테스트는 실제 AI를 부르지 않는다. 가짜 응답(fake)과 서버 메모리 저장소로 실행한다.

실제 AI 호출은 비용이 들고, 느리고, 같은 질문에도 답이 달라서 통과와 실패를 판정할 수 없다.
"""
import os

os.environ.update({"LLM_PROVIDER": "fake", "LLM_API_KEY": "", "STORAGE": "memory", "AUTH": "none"})
# 노트북의 .env 에 Langfuse 키가 있어도 테스트 기록은 보내지 않는다(빈 값이 먼저 있으면 .env 값을 덮어쓰지 않음)
os.environ.update({"LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": ""})
os.environ.setdefault("ENV", "test")
# 기존 테스트는 한 사용자(local-user)로 메시지를 많이 보낸다. 크레딧 테스트는 사용자를 따로 만들어 쓴다
os.environ["FREE_CREDITS"] = "100000"
os.environ["FREE_PER_IP_PER_DAY"] = "100000"  # 테스트는 모두 같은 주소(testclient)에서 온다
os.environ.update({"TOSS_CLIENT_KEY": "test_gck_dummy", "TOSS_SECRET_KEY": "test_gsk_dummy"})
