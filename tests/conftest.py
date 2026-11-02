"""테스트는 실제 AI를 부르지 않는다. 가짜 응답(fake)과 서버 메모리 저장소로 실행한다.

실제 AI 호출은 비용이 들고, 느리고, 같은 질문에도 답이 달라서 통과와 실패를 판정할 수 없다.
"""
import os

os.environ.update({"LLM_PROVIDER": "fake", "LLM_API_KEY": "", "STORAGE": "memory", "AUTH": "none"})
# 노트북의 .env 에 Langfuse 키가 있어도 테스트 기록은 보내지 않는다(빈 값이 먼저 있으면 .env 값을 덮어쓰지 않음)
os.environ.update({"LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": ""})
os.environ.setdefault("ENV", "test")
