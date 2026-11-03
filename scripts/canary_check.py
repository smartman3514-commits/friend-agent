"""카나리 단계에서 새 리비전을 더 넓힐지 판단하는 스크립트 (배포 워크플로에서 실행).

서비스 주소의 /version 에 0.2초마다 요청을 보내고(답을 기다리지 않고 다음 요청을 보낸다),
응답한 리비전별로 실패 수와 응답 시간을 센 뒤 아래 롤백 기준으로 판단한다.
  - 실패율(오류 응답, 연결 실패, 2초 초과)이 1%를 넘으면 실패
  - 새 리비전의 느린 쪽 응답 시간(p95)이 1초를 넘고 이전 리비전의 2배 이상이면 실패
  - 새 리비전이 받은 응답이 30건보다 적으면 판단 근거가 없으므로 실패
요청이 실패하면 0.5초 뒤 한 번만 다시 보내고, 두 번 다 실패해야 실패로 센다(재시도로 넘긴 수는 따로 적는다).
2026-11-02·03 staging 카나리가 "실패 44건(7.3%)", "64건(10.7%)"으로 두 번 롤백했는데, Cloud Run 요청 기록은
599건 모두 200·빠름이었다. 러너와 서비스 사이에서 한 번씩 끊기는 연결을 서비스 실패로 센 것이라, 한 번은 다시 묻는다.
진짜 고장(계속 500, 계속 연결 안 됨)은 두 번 다 실패하므로 그대로 잡힌다.
통과하면 0, 실패하면 1로 끝난다. 표준 라이브러리만 쓴다(러너에 따로 설치하지 않음).

실행: python3 scripts/canary_check.py <서비스 주소> <새 리비전> <이전 리비전> --seconds 120
"""
import argparse
import json
import os
import threading
import time
import urllib.request
from collections import Counter

TIMEOUT = 2.0
RETRY_DELAY = 0.5
MAX_FAIL_RATE = 0.01
P95_LIMIT = 1.0
P95_RATIO = 2.0
MIN_SAMPLES = 30


def p95(values):
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * 0.95))]


def tally(reasons):
    """['ReadTimeout', 'ReadTimeout', 'URLError'] → 'ReadTimeout 2, URLError 1'."""
    return ", ".join(f"{k} {v}" for k, v in Counter(reasons).most_common()) or "없음"


def main():
    parser = argparse.ArgumentParser(description="카나리 단계 판단")
    parser.add_argument("url")
    parser.add_argument("new_revision")
    parser.add_argument("old_revision")
    parser.add_argument("--seconds", type=int, default=120)
    args = parser.parse_args()
    url = args.url.rstrip("/") + "/version"

    latencies = {}            # 리비전 이름 -> 응답 시간 목록
    failures = []             # 실패 이유(두 번 다 실패한 것)
    retried = []              # 첫 요청이 실패했지만 다시 물어 성공한 것의 첫 실패 이유
    lock = threading.Lock()

    def ask():
        started = time.time()
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            revision = json.loads(r.read().decode()).get("revision", "?")
        return revision, time.time() - started

    def one():
        try:
            revision, elapsed = ask()
        except Exception as first:  # noqa: BLE001 — 오류 응답, 연결 실패, 시간 초과
            time.sleep(RETRY_DELAY)
            try:
                revision, elapsed = ask()
            except Exception as error:  # noqa: BLE001 — 두 번 다 실패해야 실패로 센다
                with lock:
                    failures.append(type(error).__name__)
                return
            with lock:
                retried.append(type(first).__name__)
        with lock:
            if elapsed > TIMEOUT:
                failures.append("2초 초과")
            latencies.setdefault(revision, []).append(elapsed)

    threads = []
    end = time.time() + args.seconds
    while time.time() < end:
        t = threading.Thread(target=one)
        t.start()
        threads.append(t)
        time.sleep(0.2)
    for t in threads:
        t.join()

    total = len(threads)
    new = latencies.get(args.new_revision, [])
    old = latencies.get(args.old_revision, [])
    fail_rate = len(failures) / max(total, 1)
    new_p95, old_p95 = p95(new), p95(old)

    reasons = []
    if fail_rate > MAX_FAIL_RATE:
        reasons.append(f"실패율 {fail_rate:.1%} > {MAX_FAIL_RATE:.0%}")
    if new_p95 > P95_LIMIT and new_p95 >= P95_RATIO * max(old_p95, 0.001):
        reasons.append(f"새 리비전 p95 {new_p95:.2f}초 > {P95_LIMIT:.0f}초, 이전 {old_p95:.2f}초의 {P95_RATIO:.0f}배 이상")
    if len(new) < MIN_SAMPLES:
        reasons.append(f"새 리비전 응답 {len(new)}건 < {MIN_SAMPLES}건 (판단 근거 부족)")

    lines = [
        f"요청 {total}건, 실패 {len(failures)}건 ({fail_rate:.1%}) — 이유: {tally(failures)}",
        f"재시도 후 성공 {len(retried)}건 — 첫 실패 이유: {tally(retried)}",
        f"새 리비전 {args.new_revision}: {len(new)}건, p95 {new_p95:.2f}초",
        f"이전 리비전 {args.old_revision}: {len(old)}건, p95 {old_p95:.2f}초",
        "판단: " + ("통과" if not reasons else "롤백 — " + "; ".join(reasons)),
    ]
    print("\n".join(lines))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("\n".join(f"- {line}" for line in lines) + "\n")
    raise SystemExit(1 if reasons else 0)


if __name__ == "__main__":
    main()
