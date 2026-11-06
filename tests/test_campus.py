import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from kwcampus import campus
from kwcampus.fetch import FetchResult

FIX = Path(__file__).parent / "fixtures"
MENU = (FIX / "cafeteria_week.html").read_text()
CAL = (FIX / "academic_calendar_2026.html").read_text()
MONDAY = datetime(2026, 11, 2, 22, 30, tzinfo=UTC)  # Tue 07:30 KST
THURSDAY = datetime(2026, 11, 4, 22, 30, tzinfo=UTC)  # Thu 07:30 KST


def test_parse_cafeteria_week():
    days = campus.parse_cafeteria(MENU)
    assert [d["date"] for d in days] == ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"]
    first = days[0]["meals"][0]
    assert first["name"] == "함지마루 천원의 아침" and first["price"] == "1,000원" and first["time"] == "8:30 ~ 9:30"
    assert "잡곡밥" in first["menu"] and "배추김치" in first["menu"]


def test_parse_calendar_ranges_and_single_days():
    events = campus.parse_calendar(CAL, 2026)
    assert len(events) >= 40
    signup = next(e for e in events if e["title"] == "2026학년도 1학기 수강신청")
    assert (signup["start"], signup["end"]) == ("2026-02-11", "2026-02-12")
    single = next(e for e in events if "입학식" in e["title"])
    assert single["start"] == single["end"] == "2026-02-20"
    assert all(e["start"] <= e["end"] for e in events)


def test_range_crossing_new_year():
    assert campus._range("12.22(월) ~ 01.02(금)", 2026) == ("2026-12-22", "2027-01-02")


def _enable(aws):
    aws["ssm"].put_parameter(Name="/kw/collector/enabled", Value="true", Type="String")


class Clock:
    """Real time as the Lambda sees it: sleeping moves it forward."""

    def __init__(self, start):
        self.now, self.slept = start, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


def test_run_fetches_menu_and_calendar_with_real_spacing(aws, monkeypatch):
    _enable(aws)
    calls, sleeps = [], []

    def fake_fetch(url, form=None):
        calls.append((url, form))
        return FetchResult(MENU if "facility11" in url else CAL, None, 200)

    monkeypatch.setattr(campus, "fetch_list_page", fake_fetch)
    clock = Clock(MONDAY)
    out = campus.run(MONDAY, aws["public"], clock=clock, sleep=clock.sleep)
    assert out == {"status": "ok", "cafeteria": "5 days", "calendar": f"{len(campus.parse_calendar(CAL, 2026))} events"}
    assert calls[1] == (campus.CALENDAR_URL, {"sy": "2026", "sm": ""})
    assert clock.slept == [61.0]

    assert campus.menu_for(aws["public"], date(2026, 11, 5))["weekday"] == "목요일"
    assert campus.calendar_events(aws["public"], 2026)

    # Thursday: both are fresh, so no request at all
    calls.clear()
    out = campus.run(THURSDAY, aws["public"], clock=lambda: THURSDAY, sleep=sleeps.append)
    assert out == {"status": "ok"} and calls == []


def test_run_does_nothing_when_disabled(aws):
    assert campus.run(MONDAY, aws["public"]) == {"status": "disabled"}


def test_december_also_keeps_next_years_calendar_on_another_run(aws, monkeypatch):
    _enable(aws)
    calls = []

    def fake_fetch(url, form=None):
        calls.append((url, form))
        return FetchResult(MENU if "facility11" in url else CAL, None, 200)

    monkeypatch.setattr(campus, "fetch_list_page", fake_fetch)
    december = datetime(2026, 11, 30, 22, 30, tzinfo=UTC)  # Tue 1 Dec 07:30 KST
    clock = Clock(december)
    out = campus.run(december, aws["public"], clock=clock, sleep=clock.sleep)
    assert len(calls) == 2 and [form for _, form in calls if form] == [{"sy": "2026", "sm": ""}]
    assert clock.slept == [61.0] and out["calendar_next_year"] == "next_run"  # at most two www requests a run
    calls.clear()
    tomorrow = december + timedelta(days=1)
    clock = Clock(tomorrow)
    out = campus.run(tomorrow, aws["public"], clock=clock, sleep=clock.sleep)
    # the menu (still last month's page) and next year's calendar: two requests, 61 s apart
    assert [form for _, form in calls if form] == [{"sy": "2027", "sm": ""}] and len(calls) == 2
    assert clock.slept == [61.0] and "calendar" not in out
    assert "calendar_next_year" in out and campus.calendar_events(aws["public"], 2027)


def test_last_weeks_menu_on_monday_is_stored_under_its_own_week_and_tried_again(aws, monkeypatch):
    # 07:30 Monday 9 Nov, the page still shows 2-6 Nov: not this week's menu.
    _enable(aws)
    calls = []
    pages = {"menu": MENU}

    def fake_fetch(url, form=None):
        calls.append(url)
        return FetchResult(pages["menu"] if "facility11" in url else CAL, None, 200)

    monkeypatch.setattr(campus, "fetch_list_page", fake_fetch)
    monday = datetime(2026, 11, 8, 22, 30, tzinfo=UTC)
    clock = Clock(monday)
    out = campus.run(monday, aws["public"], clock=clock, sleep=clock.sleep)
    assert out["cafeteria"] == "stale_week"
    assert campus.menu_for(aws["public"], date(2026, 11, 9)) is None
    assert campus.menu_for(aws["public"], date(2026, 11, 5))["weekday"] == "목요일"  # kept as last week's

    # Tuesday 07:30: the new week is up and is fetched (one request; the calendar is fresh)
    pages["menu"] = MENU
    for old, new in (("06", "13"), ("05", "12"), ("04", "11"), ("03", "10"), ("02", "09")):
        pages["menu"] = pages["menu"].replace(f"2026-11-{old}", f"2026-11-{new}")
    calls.clear()
    tuesday = monday + timedelta(days=1)
    clock = Clock(tuesday)
    out = campus.run(tuesday, aws["public"], clock=clock, sleep=clock.sleep)
    assert calls == [campus.CAFETERIA_URL] and out["cafeteria"] == "5 days" and clock.slept == []
    assert campus.menu_for(aws["public"], date(2026, 11, 10))["date"] == "2026-11-10"

    # Wednesday: this week is there with meals, so no request at all
    calls.clear()
    wednesday = tuesday + timedelta(days=1)
    assert campus.run(wednesday, aws["public"], clock=Clock(wednesday)) == {"status": "ok"} and calls == []


def test_a_week_without_meals_does_not_count(aws, monkeypatch):
    _enable(aws)
    empty = re.sub(r"<pre>.*?</pre>", "<pre></pre>", MENU, flags=re.DOTALL)  # the table, but no dishes yet
    assert campus.parse_cafeteria(empty) and not any(d["meals"] for d in campus.parse_cafeteria(empty))
    monkeypatch.setattr(campus, "fetch_list_page", lambda url, form=None: FetchResult(
        empty if "facility11" in url else CAL, None, 200))
    clock = Clock(MONDAY)
    out = campus.run(MONDAY, aws["public"], clock=clock, sleep=clock.sleep)
    assert out["cafeteria"] == "no_meals" and out["calendar"].endswith("events")
    calls = []
    monkeypatch.setattr(campus, "fetch_list_page", lambda url, form=None: calls.append(url) or FetchResult(
        empty, None, 200))
    clock = Clock(MONDAY + timedelta(days=1))
    campus.run(clock(), aws["public"], clock=clock, sleep=clock.sleep)
    assert calls == [campus.CAFETERIA_URL]  # still no week: asked again the next morning


def test_no_sleep_and_no_more_requests_once_the_gate_says_no(aws, monkeypatch):
    from kwcampus import politeness
    from kwcampus.kwnotice.source import NOTICE_HOST

    _enable(aws)
    calls = []
    monkeypatch.setattr(campus, "fetch_list_page", lambda url, form=None: calls.append(url))
    politeness.record_block(aws["public"], NOTICE_HOST, MONDAY, "rate_limit")  # www is cooling down
    clock = Clock(MONDAY)
    out = campus.run(MONDAY, aws["public"], clock=clock, sleep=clock.sleep)
    assert out == {"status": "ok", "cafeteria": "cooldown", "calendar": "cooldown"}
    assert calls == [] and clock.slept == []


def test_a_late_campus_run_skips_instead_of_being_cut_off(aws, monkeypatch):
    _enable(aws)
    calls = []

    def fake_fetch(url, form=None):
        calls.append(url)
        return FetchResult(MENU if "facility11" in url else CAL, None, 200)

    monkeypatch.setattr(campus, "fetch_list_page", fake_fetch)
    clock = Clock(MONDAY)
    out = campus.run(MONDAY, aws["public"], clock=clock, sleep=clock.sleep, time_left=lambda: 100)
    assert calls == [campus.CAFETERIA_URL] and out["calendar"] == "no_time" and clock.slept == []
