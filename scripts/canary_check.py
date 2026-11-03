"""카나리 단계에서 새 리비전을 더 넓힐지 판단하는 스크립트 (배포 워크플로에서 실행).

서비스 주소의 /version 에 0.2초마다 요청을 보내(답을 기다리지 않고 다음 요청을 보낸다) 두 리비전에 트래픽을 만든 뒤,
**Cloud Run 요청 기록(서버 쪽)** 으로 리비전별 요청 수, 오류 수, 응답 시간을 세어 아래 롤백 기준으로 판단한다.
  - 새 리비전의 오류율(5xx, 429)이 1%를 넘으면 실패
  - 새 리비전의 느린 쪽 응답 시간(p95)이 1초를 넘고 이전 리비전의 2배 이상이면 실패
  - 새 리비전이 받은 요청이 30건보다 적으면 판단 근거가 없으므로 실패
서버 기록을 읽지 못하면(권한, 일시 오류) 러너가 잰 값(오류 응답, 연결 실패, 2초 초과)으로 같은 기준을 적용한다.

왜 서버 기록인가: 2026-11-02·03 staging 카나리가 러너 쪽 시간초과로 네 번 롤백했다(7.3%, 10.7%, 7.5%, 15.2%).
그때마다 Cloud Run 요청 기록은 전부 200·빠름이었고, 같은 이미지 재실행은 0건이었다. 0.5초 뒤 한 번 다시 묻는 것으로도
못 막았다(러너와 서비스 사이가 몇 초씩 통째로 막힘). 사용자가 겪는 것은 서버 기록 쪽이므로 그것으로 판정하고,
러너 쪽 실패는 참고로 적는다.
통과하면 0, 실패하면 1로 끝난다. 표준 라이브러리와 gcloud 만 쓴다(러너에 따로 설치하지 않음).

실행: python3 scripts/canary_check.py <서비스 주소> <새 리비전> <이전 리비전> --seconds 120 --service <서비스 이름> --project <프로젝트>
"""
import argparse
import json
import os
import subprocess
import threading
import time
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone

TIMEOUT = 2.0
RETRY_DELAY = 0.5
MAX_FAIL_RATE = 0.01
P95_LIMIT = 1.0
P95_RATIO = 2.0
MIN_SAMPLES = 30
LOG_WAIT = 15      # 요청 기록은 몇십 초 늦게 쌓인다
LOG_POLLS = 6


def p95(values):
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * 0.95))]


def tally(reasons):
    """['ReadTimeout', 'ReadTimeout', 'URLError'] → 'ReadTimeout 2, URLError 1'."""
    return ", ".join(f"{k} {v}" for k, v in Counter(reasons).most_common()) or "없음"


def parse_logs(entries):
    """gcloud logging read 의 JSON 목록 → {리비전: {"count", "errors", "latencies"}}."""
    stats = {}
    for e in entries:
        rev = e.get("resource", {}).get("labels", {}).get("revision_name")
        req = e.get("httpRequest") or {}
        if not rev or "status" not in req:
            continue
        s = stats.setdefault(rev, {"count": 0, "errors": 0, "latencies": []})
        s["count"] += 1
        status = int(req["status"])
        s["errors"] += status >= 500 or status == 429
        if str(req.get("latency", "")).endswith("s"):
            s["latencies"].append(float(req["latency"][:-1]))
    return stats


def server_stats(service, project, revisions, since, until, expect):
    """Cloud Run 요청 기록에서 리비전별 통계를 읽는다. 보낸 수(expect)에 가까워지거나 더 늘지 않을 때까지 기다린다.
    읽지 못하면 None(러너 측정으로 판단)."""
    revs = " OR ".join(f'resource.labels.revision_name="{r}"' for r in revisions)
    filt = (f'resource.type="cloud_run_revision" AND resource.labels.service_name="{service}" AND ({revs}) '
            f'AND httpRequest.status>0 AND timestamp>="{since}" AND timestamp<="{until}"')
    cmd = ["gcloud", "logging", "read", filt, "--format=json", "--limit=20000"] + (["--project", project] if project else [])
    stats, last = None, -1
    for _ in range(LOG_POLLS):
        time.sleep(LOG_WAIT)
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=True).stdout
            stats = parse_logs(json.loads(out or "[]"))
        except Exception as error:  # noqa: BLE001 — 권한이 없거나 gcloud 가 실패하면 러너 측정으로 판단한다
            print(f"서버 요청 기록을 읽지 못했다({type(error).__name__}) — 러너가 잰 값으로 판단한다")
            return None
        got = sum(s["count"] for s in stats.values())
        if got >= expect * 0.95 or got == last:
            break
        last = got
    return stats


def verdict(rate, rate_label, new_count, new_lat, old_lat):
    """롤백 이유 목록(비면 통과)."""
    reasons = []
    if rate > MAX_FAIL_RATE:
        reasons.append(f"{rate_label} {rate:.1%} > {MAX_FAIL_RATE:.0%}")
    new_p95, old_p95 = p95(new_lat), p95(old_lat)
    if new_p95 > P95_LIMIT and new_p95 >= P95_RATIO * max(old_p95, 0.001):
        reasons.append(f"새 리비전 p95 {new_p95:.2f}초 > {P95_LIMIT:.0f}초, 이전 {old_p95:.2f}초의 {P95_RATIO:.0f}배 이상")
    if new_count < MIN_SAMPLES:
        reasons.append(f"새 리비전 요청 {new_count}건 < {MIN_SAMPLES}건 (판단 근거 부족)")
    return reasons


def main():
    parser = argparse.ArgumentParser(description="카나리 단계 판단")
    parser.add_argument("url")
    parser.add_argument("new_revision")
    parser.add_argument("old_revision")
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--service", help="Cloud Run 서비스 이름(주면 서버 요청 기록으로 판단)")
    parser.add_argument("--project")
    args = parser.parse_args()
    url = args.url.rstrip("/") + "/version"

    latencies = {}            # 리비전 이름 -> 응답 시간 목록(러너 측정)
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

    since = datetime.now(timezone.utc)
    threads = []
    end = time.time() + args.seconds
    while time.time() < end:
        t = threading.Thread(target=one)
        t.start()
        threads.append(t)
        time.sleep(0.2)
    for t in threads:
        t.join()
    until = datetime.now(timezone.utc) + timedelta(seconds=5)

    total = len(threads)
    new = latencies.get(args.new_revision, [])
    old = latencies.get(args.old_revision, [])
    lines = [f"러너 측정: 요청 {total}건, 실패 {len(failures)}건 ({len(failures) / max(total, 1):.1%}) — 이유: {tally(failures)}"
             f" · 재시도 후 성공 {len(retried)}건({tally(retried)})",
             f"  새 리비전 {len(new)}건 p95 {p95(new):.2f}초 · 이전 리비전 {len(old)}건 p95 {p95(old):.2f}초"]

    stats = None
    if args.service:
        stats = server_stats(args.service, args.project, [args.new_revision, args.old_revision],
                             since.isoformat(), until.isoformat(), total)
    if stats is not None:
        s_new = stats.get(args.new_revision, {"count": 0, "errors": 0, "latencies": []})
        s_old = stats.get(args.old_revision, {"count": 0, "errors": 0, "latencies": []})
        lines += [f"서버 기록(판정): 새 리비전 {args.new_revision}: {s_new['count']}건, 오류 {s_new['errors']}건, p95 {p95(s_new['latencies']):.2f}초",
                  f"  이전 리비전 {args.old_revision}: {s_old['count']}건, 오류 {s_old['errors']}건, p95 {p95(s_old['latencies']):.2f}초"]
        reasons = verdict(s_new["errors"] / max(s_new["count"], 1), "새 리비전 오류율", s_new["count"],
                          s_new["latencies"], s_old["latencies"])
    else:
        lines.append("판정 기준: 러너 측정(서버 기록 없음)")
        # 러너 측정은 실패를 리비전별로 나눌 수 없어 예전처럼 전체 요청 대비 실패율로 본다
        reasons = verdict(len(failures) / max(total, 1), "실패율", len(new), new, old)
    lines.append("판단: " + ("통과" if not reasons else "롤백 — " + "; ".join(reasons)))
    print("\n".join(lines))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("\n".join(f"- {line}" for line in lines) + "\n")
    raise SystemExit(1 if reasons else 0)


if __name__ == "__main__":
    main()
