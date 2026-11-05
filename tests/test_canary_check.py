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


def stats(new_count, new_errors, old_count=500):
    return {"new": {"count": new_count, "errors": new_errors, "latencies": [0.05] * new_count},
            "old": {"count": old_count, "errors": 0, "latencies": [0.05] * old_count}}


def run_with_server(monkeypatch, capsys, urlopen, server):
    monkeypatch.setattr(canary, "server_stats", lambda *a, **k: server)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(canary, "RETRY_DELAY", 0)
    monkeypatch.setattr("sys.argv", ["canary_check.py", "http://test", "new", "old", "--seconds", "1",
                                     "--service", "svc", "--project", "p"])
    with pytest.raises(SystemExit) as done:
        canary.main()
    return done.value.code, capsys.readouterr().out


def test_runner_timeouts_do_not_roll_back_when_server_is_healthy(monkeypatch, capsys):
    # 11-02·03 staging 처럼 러너는 시간초과투성이인데 서버 기록은 모두 정상인 경우
    def stuck(url, timeout):
        raise TimeoutError("timed out")

    code, out = run_with_server(monkeypatch, capsys, stuck, stats(60, 0))
    assert code == 0 and "서버 기록(판정)" in out and "TimeoutError" in out


def test_server_errors_on_new_revision_roll_back(monkeypatch, capsys):
    code, out = run_with_server(monkeypatch, capsys, lambda url, timeout: answer("new"), stats(60, 5))
    assert code == 1 and "새 리비전 오류율" in out


def test_too_few_server_requests_on_new_revision_roll_back(monkeypatch, capsys):
    code, out = run_with_server(monkeypatch, capsys, lambda url, timeout: answer("new"), stats(3, 0))
    assert code == 1 and "판단 근거 부족" in out


def test_parse_cloud_run_request_logs():
    entries = [{"resource": {"labels": {"revision_name": "r1"}}, "httpRequest": {"status": 200, "latency": "0.012s"}},
               {"resource": {"labels": {"revision_name": "r1"}}, "httpRequest": {"status": 503, "latency": "0.5s"}},
               {"resource": {"labels": {"revision_name": "r1"}}, "httpRequest": {"status": 429}},
               {"resource": {"labels": {"revision_name": "r1"}}, "textPayload": "앱 로그"}]
    s = canary.parse_logs(entries)["r1"]
    assert s["count"] == 3 and s["errors"] == 2 and s["latencies"] == [0.012, 0.5]


def busy_stats(new_count, errors, busy):
    s = stats(new_count, errors)
    s["new"]["busy"] = busy
    return s


def test_only_429_on_new_revision_asks_to_measure_again(monkeypatch, capsys):
    # 2026-11-04 운영·11-05 staging: 새 리비전이 429(인스턴스 대기)만 내고 앱 오류는 없었다 → 3(다시 재기)
    code, out = run_with_server(monkeypatch, capsys, lambda url, timeout: answer("new"), busy_stats(54, 5, 5))
    assert code == 3 and "다시 재기" in out


def test_any_5xx_on_new_revision_still_rolls_back(monkeypatch, capsys):
    # 5xx 가 하나라도 섞이면 다시 재지 않고 바로 롤백(1)
    code, out = run_with_server(monkeypatch, capsys, lambda url, timeout: answer("new"), busy_stats(54, 5, 4))
    assert code == 1 and "롤백" in out


def test_parse_counts_429_as_busy():
    entries = [{"resource": {"labels": {"revision_name": "r1"}}, "httpRequest": {"status": 429}},
               {"resource": {"labels": {"revision_name": "r1"}}, "httpRequest": {"status": 503}}]
    s = canary.parse_logs(entries)["r1"]
    assert s["errors"] == 2 and s["busy"] == 1
