"""Langfuse 트레이스 기록을 한곳에 모아 둔다.

메시지 하나가 거치는 단계(안전장치, 도구 선택, 도구 실행, 답 만들기, 기억 정리)를 관찰 단위로 남겨,
"요청은 성공했는데 답이 이상하다"는 문제가 어느 단계에서 처음 생겼는지 볼 수 있게 한다.

- LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY 가 둘 다 있을 때만 켠다. 없으면 기록하지 않고 앱은 그대로 돈다.
- 대화 원문은 Langfuse 에만 남긴다(서버 로그에는 남기지 않는 규칙은 그대로). 보내기 전에 전화번호, 이메일,
  카드 번호는 가린다. SDK의 mask 는 input, output 에는 걸리지만 metadata 안의 값까지 가리는지는 확실하지 않아서,
  mask 함수가 dict, list 안쪽까지 내려가며 가린다.
- SDK 버퍼는 기본값(512개 또는 5초)으로 모았다가 백그라운드에서 보내므로 답이 늦어지지 않는다.
근거: https://langfuse.com/docs/observability/sdk/python/instrumentation
      https://langfuse.com/docs/observability/features/masking
"""
import logging
import os
import re
from contextlib import ExitStack, contextmanager

from langfuse import Langfuse, propagate_attributes
from opentelemetry import trace as otel_trace

# 전화번호(010-1234-5678, 01012345678, 02-123-4567), 이메일, 카드 번호(4자리씩 16자리)
_PATTERNS = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[이메일]"),
    (re.compile(r"\b(?:\d{4}[- ]?){3}\d{4}\b"), "[카드번호]"),
    (re.compile(r"\b01[016789][- ]?\d{3,4}[- ]?\d{4}\b"), "[전화번호]"),
    (re.compile(r"\b0\d{1,2}-\d{3,4}-\d{4}\b"), "[전화번호]"),
]


def mask_text(text):
    for pattern, label in _PATTERNS:
        text = pattern.sub(label, text)
    return text


def mask(*, data, **kwargs):
    """Langfuse 로 보내기 전에 값 안의 개인정보 패턴을 가린다(dict, list 안쪽까지)."""
    if isinstance(data, str):
        return mask_text(data)
    if isinstance(data, dict):
        return {k: mask(data=v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [mask(data=v) for v in data]
    return data


_client = None


def lf():
    """Langfuse 클라이언트. .env 는 앱 설정을 읽을 때(load_settings) 들어오므로, 처음 쓸 때 만든다."""
    global _client
    if _client is None:
        on = enabled()
        if not on:
            # 키가 없을 때 SDK가 남기는 인증 경고는 끈 상태에서는 의미가 없어 숨긴다
            logging.getLogger("langfuse").setLevel(logging.ERROR)
        # environment(local, staging, production)로 내 컴퓨터의 시험 기록과 배포한 서비스의 기록을 나눈다
        _client = Langfuse(tracing_enabled=on, mask=mask, environment=os.getenv("ENV", "local").strip().lower())
    return _client


def enabled():
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


def turn_attributes(uid, now):
    """트레이스에 붙일 속성. 누가(사용자), 어느 대화 묶음(세션 = 사용자 + 그 사람의 날짜), 어느 배포 버전이 만들었는지.

    사용자 ID는 로그인으로 확인한 값만 쓴다(이메일은 넣지 않는다). 버전은 Cloud Run 이 넣어 주는 리비전 이름.
    """
    return {"user_id": str(uid), "session_id": f"{uid}-{now:%Y-%m-%d}",
            "version": os.getenv("K_REVISION", "local"),
            "metadata": {"commit": os.getenv("COMMIT_SHA", "local")[:12]}}


@contextmanager
def inside(trace, attrs):
    """trace(chat-turn)를 잠시 '지금 열린 관찰 단위'로 되살리고 트레이스 속성을 함께 건다.

    한 턴은 조각마다 다른 스레드에서 이어질 수 있어서, yield 사이의 블록마다 이렇게 다시 연다.
    속성은 단계마다 붙어 있어야 사용자별 비용 같은 집계에 들어간다(propagate_attributes 설명).
    SDK에 관찰 단위를 다시 여는 공개 함수가 없어 내부 값(_otel_span)을 쓴다(langfuse 5.1.0 기준).
    """
    span = getattr(trace, "_otel_span", None)
    with ExitStack() as stack:
        if span is not None:
            stack.enter_context(otel_trace.use_span(span, end_on_exit=False))
        stack.enter_context(propagate_attributes(**attrs))
        yield


@contextmanager
def child(trace, attrs, **kwargs):
    """trace 아래에 단계 하나를 열고, 그 안에서는 '지금 열린 관찰 단위'로 둔다(모델 호출이 토큰을 적을 수 있게)."""
    with inside(trace, attrs), lf().start_as_current_observation(**kwargs) as observation:
        yield observation


def flush_on_cloud_run():
    """Cloud Run 은 응답을 마친 뒤 CPU 를 거의 주지 않아 백그라운드 전송이 늦거나 빠질 수 있어, 응답을 닫기 전에 보낸다."""
    if enabled() and os.getenv("K_SERVICE"):
        lf().flush()


def trace_id(observation):
    """메시지 meta 에 저장할 트레이스 ID. 계측이 꺼져 있으면 None."""
    return observation.trace_id if enabled() else None


def score(trace, name, value, comment=None):
    """화면의 반응(좋아요 1, 별로예요 0, 신고 1)을 그 답의 트레이스에 BOOLEAN 점수로 붙인다.

    근거: https://langfuse.com/docs/evaluation/evaluation-methods/scores-via-sdk
    """
    if not enabled() or not trace:
        return
    lf().create_score(name=name, value=value, trace_id=trace, data_type="BOOLEAN",
                      comment=mask_text(comment) if comment else None)
    flush_on_cloud_run()


def record_llm(model, messages, output, input_tokens, output_tokens):
    """지금 열려 있는 generation 에 모델 호출 한 번의 내용을 적는다(llm.py 가 부른다).

    열린 generation 이 없으면(계측 밖에서 부른 경우) 아무것도 하지 않는다.
    """
    client = lf()
    if not enabled() or not client.get_current_observation_id():
        return
    client.update_current_generation(model=model, input=messages, output=output,
                                     usage_details={"input": input_tokens or 0, "output": output_tokens or 0})
