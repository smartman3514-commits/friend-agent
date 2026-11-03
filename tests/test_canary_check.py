"""카나리 판단 스크립트: 한 번 끊긴 요청은 다시 물어 넘기고, 계속 실패하면 그대로 롤백한다.

2026-11-02·03 staging 카나리가 러너 쪽 연결 끊김으로 두 번 롤백했다(서버 기록은 모두 200). 인터넷으로 보내지 않고
urlopen 을 가짜로 바꿔 본다.
"""
import importlib.util
import io
import json
import threading
import urllib.request
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("canary_check", Path(__file__).parents[1] / "scripts" / "canary_check.py")
canary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(canary)


def answer(revision):
    return io.BytesIO(json.dumps({"revision": revision}).encode())


def run(monkeypatch, capsys, urlopen):
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(canary, "MIN_SAMPLES", 1)
    monkeypatch.setattr(canary, "RETRY_DELAY", 0)
    monkeypatch.setattr("sys.argv", ["canary_check.py", "http://test", "new", "old", "--seconds", "1"])
    with pytest.raises(SystemExit) as done:
        canary.main()
    return done.value.code, capsys.readouterr().out


def test_a_dropped_request_is_asked_again(monkeypatch, capsys):
    seen = threading.local()

    def flaky(url, timeout):  # 요청마다 첫 번째는 끊기고 두 번째는 성공
        if not getattr(seen, "tried", False):
            seen.tried = True
            raise TimeoutError("read timed out")
        return answer("new")

    code, out = run(monkeypatch, capsys, flaky)
    assert code == 0
    assert "실패 0건" in out and "재시도 후 성공" in out and "TimeoutError" in out


def test_a_revision_that_keeps_failing_is_rolled_back(monkeypatch, capsys):
    def broken(url, timeout):
        raise ConnectionError("refused")

    code, out = run(monkeypatch, capsys, broken)
    assert code == 1
    assert "롤백" in out and "ConnectionError" in out
