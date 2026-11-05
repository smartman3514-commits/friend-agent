# 11-05: 손글씨 일정 사진을 tesseract 가 조각만 읽어('우 3시 A 과') AI 가 사진을 보고 옮겨 적게 바꿨다.
# 사진 한 장에 크레딧 3개를 먼저 빼고, 읽기에 실패하면 돌려준다. 모자라면 402.
import io
import uuid

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import credits, ocr
from app.auth import User
from app.main import app, current_user, get_llm, settings, store

client = TestClient(app)


def photo_bytes(size=(3000, 2000)):
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, "JPEG")
    return buf.getvalue()


class VisionStub:
    model = "vision-stub"

    def __init__(self, text="11월 20일 오후 3시 치과", fail=False):
        self.text, self.fail, self.calls = text, fail, []

    def read_image(self, instruction, jpeg):
        self.calls.append((instruction, Image.open(io.BytesIO(jpeg)).size))
        if self.fail:
            raise RuntimeError("model down")
        return self.text


@pytest.fixture
def user(monkeypatch):
    monkeypatch.setattr(credits, "FREE_CREDITS", 30)
    monkeypatch.setattr(settings, "ocr_enabled", True)
    uid = f"ocr-{uuid.uuid4().hex[:8]}"
    app.dependency_overrides[current_user] = lambda: User(uid, anonymous=True)
    yield uid
    app.dependency_overrides.pop(current_user, None)
    app.dependency_overrides.pop(get_llm, None)


def send_photo():
    return client.post("/api/ocr", files={"photo": ("p.jpg", photo_bytes(), "image/jpeg")})


def test_ai_reads_the_photo_and_three_credits_are_spent(user):
    stub = VisionStub()
    app.dependency_overrides[get_llm] = lambda: stub
    r = send_photo()
    assert r.status_code == 200 and r.json()["text"] == "11월 20일 오후 3시 치과"
    assert r.json()["credits_used"] == 3 and store.credit_balance(user) == 27
    instruction, size = stub.calls[0]
    assert "옮겨 적어" in instruction and max(size) == ocr.VISION_LONG_SIDE   # 큰 사진은 줄여서 보낸다


def test_failed_reading_gives_the_credits_back(user):
    app.dependency_overrides[get_llm] = lambda: VisionStub(fail=True)
    r = send_photo()
    assert r.status_code == 500
    assert store.credit_balance(user) == 30


def test_not_enough_credits_is_refused_before_reading(user):
    stub = VisionStub()
    app.dependency_overrides[get_llm] = lambda: stub
    credits.ensure_signup(store, user)
    store.add_credit(user, -28, "chat", f"test-adjust:{uuid.uuid4().hex}")   # 잔액 2
    r = send_photo()
    assert r.status_code == 402 and r.json()["error"] == "insufficient_credits"
    assert stub.calls == [] and store.credit_balance(user) == 2


def test_models_without_vision_fall_back_to_tesseract(monkeypatch):
    monkeypatch.setattr(ocr, "read_with_tesseract", lambda image: "tesseract 결과")
    assert ocr.read_text(photo_bytes((800, 600)), llm=object()) == "tesseract 결과"
