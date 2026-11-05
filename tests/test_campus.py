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


def test_run_fetches_menu_and_calendar_with_real_spacing(aws, monkeypatch):
    _enable(aws)
    calls, sleeps = [], []

    def fake_fetch(url, form=None):
        calls.append((url, form))
        return FetchResult(MENU if "facility11" in url else CAL, None, 200)

    monkeypatch.setattr(campus, "fetch_list_page", fake_fetch)
    out = campus.run(MONDAY, aws["public"], clock=lambda: MONDAY + timedelta(seconds=61), sleep=sleeps.append)
    assert out == {"status": "ok", "cafeteria": "5 days", "calendar": f"{len(campus.parse_calendar(CAL, 2026))} events"}
    assert calls[1] == (campus.CALENDAR_URL, {"sy": "2026", "sm": ""})
    assert sleeps == [61.0]

    assert campus.menu_for(aws["public"], date(2026, 11, 5))["weekday"] == "목요일"
    assert campus.calendar_events(aws["public"], 2026)

    # Thursday: both are fresh, so no request at all
    calls.clear()
    out = campus.run(THURSDAY, aws["public"], clock=lambda: THURSDAY, sleep=sleeps.append)
    assert out == {"status": "ok"} and calls == []


def test_run_does_nothing_when_disabled(aws):
    assert campus.run(MONDAY, aws["public"]) == {"status": "disabled"}
