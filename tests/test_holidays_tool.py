# 11-03 friend-reply 실험 R05: 기간에 공휴일이 없다는 결과만 주면 답이 "다음 공휴일은 12월"처럼 지어냈다.
# 공휴일 도구가 그 뒤의 첫 공휴일도 함께 돌려주는지 본다(공휴일 API 는 부르지 않고 가짜 목록을 쓴다).
from datetime import date

from app import tools
from app.tools import Context, get_holidays
from app.store.memory_store import MemoryStore

FAKE = {2026: [(date(2026, 10, 9), "한글날"), (date(2026, 12, 25), "크리스마스")], 2027: [(date(2027, 1, 1), "새해")]}


def ctx():
    return Context(MemoryStore(), "u", "서울", "Asia/Seoul")


def test_empty_range_also_names_the_next_holiday(monkeypatch):
    monkeypatch.setattr(tools, "holidays_of", lambda year: FAKE.get(year, []))
    r = get_holidays({"from": "2026-11-02", "to": "2026-11-30"}, ctx())
    assert r.ok and "2026-11-02부터 2026-11-30까지 공휴일이 없습니다." in r.summary
    assert "그 뒤 첫 공휴일은 12월 25일(금) 크리스마스입니다." in r.summary
    assert r.data["next_holiday"] == {"date": "2026-12-25", "name": "크리스마스"}


def test_next_holiday_lookup_failure_keeps_the_range_answer(monkeypatch):
    def holidays_of(year):
        if year == 2026:
            return FAKE[2026]
        raise RuntimeError("API down")
    monkeypatch.setattr(tools, "holidays_of", holidays_of)
    r = get_holidays({"from": "2026-12-26", "to": "2026-12-31"}, ctx())
    assert r.ok and r.summary == "2026-12-26부터 2026-12-31까지 공휴일이 없습니다."


def test_friday_holiday_is_reported_as_a_three_day_weekend(monkeypatch):
    monkeypatch.setattr(tools, "holidays_of", lambda year: FAKE.get(year, []))
    r = get_holidays({"from": "2026-12-01", "to": "2026-12-31"}, ctx())
    assert "연휴: 12월 25일(금)부터 12월 27일(일)까지 3일." in r.summary
    assert r.data["long_weekends"] == [{"from": "2026-12-25", "to": "2026-12-27", "days": 3}]


def test_midweek_holiday_is_not_a_long_weekend(monkeypatch):
    monkeypatch.setattr(tools, "holidays_of", lambda year: {2026: [(date(2026, 10, 7), "평일 공휴일")]}.get(year, []))
    r = get_holidays({"from": "2026-10-01", "to": "2026-10-31"}, ctx())
    assert "연휴" not in r.summary and r.data["long_weekends"] == []


def test_back_to_back_holidays_join_the_weekend(monkeypatch):
    # 2026 추석처럼 공휴일이 붙어 있고 주말과 이어지면 한 덩어리로 센다(9/24 목 ~ 9/27 일)
    days = [(date(2026, 9, 24), "추석 연휴"), (date(2026, 9, 25), "추석"), (date(2026, 9, 26), "추석 연휴")]
    monkeypatch.setattr(tools, "holidays_of", lambda year: {2026: days}.get(year, []))
    r = get_holidays({"from": "2026-09-01", "to": "2026-09-30"}, ctx())
    assert r.data["long_weekends"] == [{"from": "2026-09-24", "to": "2026-09-27", "days": 4}]
