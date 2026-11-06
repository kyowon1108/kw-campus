"""robots.txt: every collected host has a recorded check, and a Disallow'd path is never fetched."""

from datetime import UTC, date, datetime

from kwcampus import (
    aicon,
    campus,
    collector,
    deptboards,
    politeness,
    wixfeeds,
    wwwboards,
)
from kwcampus.fetch import FetchResult
from kwcampus.kwnotice.source import build_list_url

NOW = datetime(2026, 11, 5, 3, 0, tzinfo=UTC)


def collected_urls() -> dict[str, str]:
    """Every page a collector asks for, by source."""
    return {
        "kw-notice": str(build_list_url(1)),
        **{b.source: b.list_url for b in wwwboards.BOARDS},
        "campus-cafeteria": campus.CAFETERIA_URL,
        "campus-calendar": campus.CALENDAR_URL,
        "aicon": aicon.WIDGET_URL,
        **{b.source: b.list_url for b in deptboards.BOARDS},
        **{f.source: f.url for f in wixfeeds.FEEDS},
    }


def test_every_source_has_a_robots_record_and_is_allowed():
    for source, url in collected_urls().items():
        host = url.split("/")[2]
        assert host in politeness.ROBOTS, f"{source}: check https://{host}/robots.txt by hand and record it"
        check = politeness.ROBOTS[host]
        assert date.fromisoformat(check.checked) and check.result
        assert politeness.robots_allows(url), f"{source}: {url} is disallowed by robots.txt; remove the source"


def test_rules_and_unknown_hosts():
    assert not politeness.robots_allows("https://unchecked.kw.ac.kr/notice.php")  # fail closed
    # job.kw.ac.kr (same server as E1, not collected) disallows its boards
    assert not politeness.robots_allows("https://job.kw.ac.kr/notice/news.php?page=1")
    assert not politeness.robots_allows("https://job.kw.ac.kr/job/list.php")
    assert politeness.robots_allows("https://job.kw.ac.kr/")
    # Wix: everything but lightbox URLs
    assert not politeness.robots_allows("https://www.kwmedia.info/post/x?lightbox=abc")
    assert politeness.robots_allows("https://www.kwmedia.info/blog-feed.xml")


def test_a_disallowed_board_is_a_disabled_source(aws, monkeypatch):
    ee = next(b for b in deptboards.BOARDS if b.key == "ee")
    monkeypatch.setattr(deptboards, "BOARDS", (ee,))
    monkeypatch.setitem(politeness.ROBOTS, ee.host, politeness.RobotsCheck("2026-11-07", "Disallow: /HTML/", ("/HTML/",)))
    calls = []
    out = deptboards.collect(NOW, aws["public"], fetch=lambda url, **kw: calls.append(url))
    assert out == {deptboards.SERVER_E1: {"status": "skipped", "reason": "no_board_due"}} and calls == []


def test_disallowed_main_board_feed_and_widget_are_skipped(aws, monkeypatch):
    aws["ssm"].put_parameter(Name="/kw/collector/enabled", Value="true", Type="String")
    for host in ("www.kw.ac.kr", "aicon.kw.ac.kr", *(f.host for f in wixfeeds.FEEDS)):
        monkeypatch.setitem(politeness.ROBOTS, host, politeness.RobotsCheck("2026-11-07", "Disallow: /", ("/",)))
    calls = []

    def fetch(url, **kw):
        calls.append(url)
        return FetchResult(None, "transport_failure", 0)

    assert collector.run(NOW, aws["public"], fetch=fetch) == {"status": "skipped", "reason": "robots"}
    assert aicon.collect(NOW, aws["public"], fetch=fetch) == {"status": "skipped", "reason": "robots"}
    feeds = wixfeeds.collect(NOW, aws["public"], fetch=fetch)
    assert feeds == {f.key: {"status": "skipped", "reason": "robots"} for f in wixfeeds.FEEDS}
    assert wwwboards.collect(NOW, aws["public"], fetch=fetch, clock=lambda: NOW, sleep=lambda s: None) == {"status": "not_due"}
    assert calls == []
