"""robots.txt: every collected host has a recorded check, and a Disallow'd path is never fetched."""

import re
from datetime import UTC, date, datetime

from kwcampus import (
    aicon,
    campus,
    collector,
    deptboards,
    politeness,
    smallboards,
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
        **{b.source: b.list_url for b in smallboards.BOARDS},
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
    # Allow beats a shorter Disallow (the longest rule wins; an Allow on a tie), and dot segments are resolved
    assert politeness.robots_allows("https://internship.kw.ac.kr/index.do")
    assert not politeness.robots_allows("https://internship.kw.ac.kr/home/notice/view.do?bbsSeq=1")
    assert not politeness.robots_allows("https://internship.kw.ac.kr/index.do/../home/notice/index.do")
    assert not politeness.robots_allows("https://www.kwmedia.info/post/a/../x?lightbox=1")
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


def _allowed_before(url: str) -> bool:
    """robots_allows as it was before Allow rules and path resolving: Disallow prefixes on the raw path."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    check = politeness.ROBOTS.get(parts.hostname or "")
    if check is None:
        return False
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return not any(politeness._rule(r if r.startswith("/") else "/" + r).match(target) for r in check.disallow)


def _paths(check: politeness.RobotsCheck) -> list[str]:
    paths = ["/", "/index.do", "/community/notice.php?BoardMode=view&UID=1", "/bbs/board.php?bo_table=notice&wr_id=1",
             "/notices", "/blog-feed.xml", "/post/x?lightbox=1", "/a/../b", "/a/./b", "/a/%2e%2e/b", "//b", "/a;b"]
    for rule in check.disallow + check.allow:
        r = "/" + rule.strip("*$").lstrip("/")
        paths += [r, r + "x", r.rstrip("/"), r + "%2e", r + "%2E", r + ".", r + "..", r + "/..", "/a/.." + r,
                  r.replace("/", "/./", 1), "/x/.." + r, "/%2e%2e" + r, r + "?q=1", "/" + r, r + ";x",
                  r.rstrip("/") + "/%2e", r.rstrip("/") + "/%2E/x"]
    return list(dict.fromkeys(paths))


def test_robots_never_allows_more_than_before_on_any_recorded_host():
    """Against every host in the table: a path the old Disallow-only check refused stays refused, and a plain
    path (no dot segments, escapes, "//" or ";") gets the same answer as before. Only a host with Allow rules
    (현장실습) opens anything, and only its exact Allow."""
    plain = re.compile(r"^(?!.*(?:/\.\.?(?:/|$|\?)|%|//|;))/")
    for host, check in politeness.ROBOTS.items():
        for path in _paths(check):
            url = f"https://{host}{path}"
            now, before = politeness.robots_allows(url), _allowed_before(url)
            if check.allow:
                assert now == (path == "/index.do"), url
                continue
            assert not now or before, f"looser than before: {url}"
            if plain.match(path):
                assert now == before, url


def test_job_kw_disallowed_paths_stay_closed_under_dot_segments_and_escapes():
    for path in ("/internship/%2e", "/internship/%2E", "/internship/.", "/mobile/recommend/%2E", "/mobile/recommend/.",
                 "/job/x/..", "/recommend/%2e%2e/", "/community/../job/list.php", "/x/%2e%2e/recommend/a",
                 "/internship/"):
        assert not politeness.robots_allows(f"https://job.kw.ac.kr{path}"), path
    assert politeness._resolved("/internship/%2e") == "/internship/" and politeness._resolved("/a/b/..") == "/a/"
    assert politeness._resolved("/..") == "/" and politeness._resolved("/a/./b/../c") == "/a/c"
    assert politeness.robots_allows("https://job.kw.ac.kr/community/notice.php?BoardMode=view&UID=1")
    assert politeness.robots_allows("https://job.kw.ac.kr/internship")  # "Disallow: /internship/" is a prefix rule
