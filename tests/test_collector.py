from datetime import UTC, datetime, timedelta
from pathlib import Path

from kwcampus import collector, politeness
from kwcampus.fetch import FetchResult
from kwcampus.kwnotice.source import NOTICE_HOST

FIXTURE = (Path(__file__).parent / "fixtures" / "board_list_live_shape_sanitized.html").read_text()
NOW = datetime(2026, 11, 5, 3, 0, tzinfo=UTC)  # 12:00 KST


def ok_fetch(url):
    assert url.startswith("https://www.kw.ac.kr/ko/life/notice.jsp?")
    return FetchResult(FIXTURE, None, 200)


def enable(aws):
    aws["ssm"].put_parameter(Name="/kw/collector/enabled", Value="true", Type="String")


def notices(table):
    return [i for i in table.scan()["Items"] if i["SK"].startswith("N#")]


def test_does_nothing_until_switched_on(aws):
    calls = []
    result = collector.run(NOW, aws["public"], fetch=lambda u: calls.append(u))
    assert result == {"status": "disabled"}
    assert calls == []


def test_first_run_seeds_then_later_runs_are_quiet(aws):
    enable(aws)
    first = collector.run(NOW, aws["public"], fetch=ok_fetch)
    assert first["status"] == "ok" and first["seed_run"] and first["inserted"] > 0
    rows = notices(aws["public"])
    assert rows and all(r["seed"] for r in rows)
    assert all(set(r) >= {"title", "url", "category_id", "posted_date", "ttl"} for r in rows)

    second = collector.run(NOW + timedelta(minutes=20), aws["public"], fetch=ok_fetch)
    assert second["status"] == "ok" and not second["seed_run"]
    assert second["inserted"] == 0 and second["updated"] == 0


def test_gate_enforces_interval_between_requests(aws):
    enable(aws)
    collector.run(NOW, aws["public"], fetch=ok_fetch)
    calls = []
    result = collector.run(NOW + timedelta(seconds=30), aws["public"], fetch=lambda u: calls.append(u))
    assert result == {"status": "skipped", "reason": "too_soon"}
    assert calls == []


def test_block_starts_cooldown_and_stops_requests(aws):
    enable(aws)
    blocked = collector.run(NOW, aws["public"], fetch=lambda u: FetchResult(None, "rate_limit", 429))
    assert blocked == {"status": "blocked", "reason": "rate_limit"}
    calls = []
    later = collector.run(NOW + timedelta(minutes=20), aws["public"], fetch=lambda u: calls.append(u))
    assert later["reason"] == "cooldown" and calls == []


def test_three_blocks_in_a_row_disable_the_origin(aws):
    t = NOW
    for _ in range(3):
        politeness.record_block(aws["public"], NOTICE_HOST, t, "captcha")
        t += timedelta(days=1)
    permit = politeness.acquire(aws["public"], NOTICE_HOST, t + timedelta(days=2), daily_cap=40)
    assert permit.reason == "disabled_after_blocks"


def test_empty_list_counts_as_block(aws):
    enable(aws)
    result = collector.run(NOW, aws["public"], fetch=lambda u: FetchResult("<html><body></body></html>", None, 200))
    assert result == {"status": "blocked", "reason": "no_rows"}


def test_daily_cap(aws):
    t = NOW
    for _ in range(3):
        assert politeness.acquire(aws["public"], "example.org", t, daily_cap=3).granted
        t += timedelta(minutes=2)
    assert politeness.acquire(aws["public"], "example.org", t, daily_cap=3).reason == "daily_cap"
