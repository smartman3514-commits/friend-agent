"""Langfuse 계측이 정한 모양대로 남는지 본다. 인터넷으로 보내지 않고 메모리에 모아 검사한다.

- 한 턴은 chat-turn 아래 safety, choose-tools, tool:<이름>, reply, memory-update 순서로 남는다.
- 모델을 부르는 단계는 generation, 나머지는 span 이다(비용 계산이 generation 에서만 되기 때문).
- 전화번호, 이메일, 카드 번호는 보내기 전에 가려진다(metadata 안쪽까지).
- 키가 없으면 계측이 꺼지고 앱은 그대로 돈다.
"""
import json

import pytest
from langfuse import Langfuse
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import agent, tracing
from app.llm import FakeLLM
from app.store.memory_store import MemoryStore


# Langfuse 는 같은 공개키의 클라이언트를 하나로 재사용하므로, 수집함(exporter)도 하나만 만들어 테스트마다 비운다
EXPORTER = InMemorySpanExporter()
CLIENT = Langfuse(public_key="pk-lf-test", secret_key="sk-lf-test", base_url="http://127.0.0.1:9",
                  span_exporter=EXPORTER, mask=tracing.mask)


@pytest.fixture
def spans(monkeypatch):
    client, exporter = CLIENT, EXPORTER
    exporter.clear()
    monkeypatch.setattr(tracing, "_client", client)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")

    def finished():
        client.flush()
        return exporter.get_finished_spans()
    yield finished
    exporter.clear()


def kind(span):
    return span.attributes.get("langfuse.observation.type")


def run(message, store):
    return list(agent.run_turn(message, "u1", FakeLLM(delay=0), store, show_panel=False))


def test_turn_steps_are_children_of_chat_turn(spans):
    run("내일 서울 날씨 어때?", MemoryStore())
    found = spans()
    root = next(s for s in found if s.name == "chat-turn")
    children = sorted((s for s in found if s.parent and s.parent.span_id == root.context.span_id),
                      key=lambda s: s.start_time)
    assert [s.name for s in children] == ["safety", "choose-tools", "tool:get_weather", "reply", "memory-update"]
    assert {s.name: kind(s) for s in children} == {
        "safety": "span", "choose-tools": "generation", "tool:get_weather": "span",
        "reply": "generation", "memory-update": "generation"}
    reply = next(s for s in children if s.name == "reply")
    assert reply.attributes["langfuse.observation.model.name"] == "fake"
    assert "langfuse.observation.completion_start_time" in reply.attributes


def test_crisis_turn_stops_after_safety(spans):
    from app.safety import CRISIS_KEYWORDS
    run(f"요즘 {CRISIS_KEYWORDS[0]}", MemoryStore())
    names = {s.name for s in spans()}
    assert "safety" in names and "reply" not in names and "choose-tools" not in names


def test_personal_patterns_are_masked_before_sending(spans):
    run("내 번호는 010-1234-5678 이고 메일은 me@example.com, 카드는 1234-5678-9012-3456 이야", MemoryStore())
    sent = json.dumps([dict(s.attributes) for s in spans()], ensure_ascii=False)
    assert "010-1234-5678" not in sent and "me@example.com" not in sent and "1234-5678-9012-3456" not in sent
    assert "[전화번호]" in sent and "[이메일]" in sent and "[카드번호]" in sent


def test_mask_reaches_inside_metadata():
    assert tracing.mask(data={"a": ["연락처 010 1234 5678"], "b": 3}) == {"a": ["연락처 [전화번호]"], "b": 3}


def test_tracing_is_off_without_keys(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "")
    assert tracing.enabled() is False
    events = run("안녕", MemoryStore())  # 꺼져 있어도 답은 그대로 나온다
    assert events[-1]["type"] == "done"


def test_keys_are_cleaned_of_spaces_and_quotes(monkeypatch):
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", ' "sk-lf-abc"\n')
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "   ")
    assert tracing._key("LANGFUSE_SECRET_KEY") == "sk-lf-abc"
    assert tracing.enabled() is False  # 공백뿐인 키는 없는 것으로 본다


def test_every_step_carries_user_session_version_and_tool_tag(spans, monkeypatch):
    monkeypatch.setenv("K_REVISION", "friend-agent-staging-00099-abc")
    run("내일 서울 날씨 어때?", MemoryStore())
    found = spans()
    assert {s.name for s in found} >= {"chat-turn", "safety", "choose-tools", "tool:get_weather", "reply", "memory-update"}
    for s in found:  # 속성이 단계마다 있어야 사용자별 비용 같은 집계에 들어간다
        assert s.attributes["user.id"] == "u1"
        assert s.attributes["session.id"].startswith("u1-")
        assert s.attributes["langfuse.version"] == "friend-agent-staging-00099-abc"
    root = next(s for s in found if s.name == "chat-turn")
    assert root.attributes["langfuse.trace.tags"] == ("get_weather",)


def test_feedback_and_report_become_scores_on_the_answer_trace(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app, store
    sent = []
    monkeypatch.setattr(tracing, "enabled", lambda: True)
    monkeypatch.setattr(tracing, "lf", lambda: type("C", (), {"create_score": lambda self, **kw: sent.append(kw),
                                                             "flush": lambda self: None})())
    answer = store.add_message("local-user", "assistant", "답", {"trace_id": "t-abc"})
    client = TestClient(app)
    assert client.post("/api/feedback", json={"message_id": answer["id"], "value": "down"}).status_code == 200
    assert client.post("/api/report", json={"message_id": answer["id"], "reason": "연락처 010-1234-5678 노출"}).status_code == 200
    assert sent[0] == {"name": "user-feedback", "value": 0, "trace_id": "t-abc", "data_type": "BOOLEAN", "comment": None}
    assert sent[1]["name"] == "report" and sent[1]["value"] == 1 and sent[1]["comment"] == "연락처 [전화번호] 노출"


def test_cannot_score_someone_elses_message():
    from fastapi.testclient import TestClient
    from app.main import app, store
    other = store.add_message("someone-else", "assistant", "남의 답", {"trace_id": "t-x"})
    client = TestClient(app)
    assert client.post("/api/feedback", json={"message_id": other["id"], "value": "up"}).status_code == 404
    assert client.post("/api/report", json={"message_id": other["id"], "reason": ""}).status_code == 404
