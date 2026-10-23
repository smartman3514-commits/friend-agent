# tests/test_production_hides_turn_info.py
import json

from fastapi.testclient import TestClient

from app import config
from app.main import app, settings

client = TestClient(app)

# 개발용 "이번 턴" 패널에만 쓰는 이벤트와 항목. production 응답에는 하나도 없어야 한다.
PANEL_EVENTS = {"turn", "tool", "context"}
PANEL_KEYS = {"tools", "confidence", "guards", "decided_by", "reason"}


def test_production_response_has_no_turn_info(monkeypatch):
    # 예시 E12: ENV=production 에서 메시지를 보낸다 → 응답에 도구, 확신도, 안전장치 정보가 들어 있지 않다
    monkeypatch.setenv("ENV", "production")
    monkeypatch.delenv("SHOW_TURN_PANEL", raising=False)
    monkeypatch.setattr(settings, "show_turn_panel", config.load_settings().show_turn_panel)

    # 날씨 도구를 고르는 메시지와 안전 응답이 나가는 메시지 둘 다 확인한다
    for message in ["내일 서울 날씨 어때?", "요즘 너무 죽고 싶어"]:
        r = client.post("/api/chat", json={"message": message})
        events = [json.loads(line) for line in r.text.splitlines()]
        assert any(e["type"] == "delta" for e in events), message
        assert not [e["type"] for e in events if e["type"] in PANEL_EVENTS], message
        assert not [k for e in events for k in e if k in PANEL_KEYS], message
