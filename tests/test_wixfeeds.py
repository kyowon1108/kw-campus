"""Department news from Wix blog RSS feeds: one request per host, at most every 2 hours."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from kwcampus import dispatcher, fetch, politeness, public_data, routing, wixfeeds
from kwcampus.fetch import FetchResult

FIXTURES = Path(__file__).parent / "fixtures"
# Saved from https://www.kwuarchitecture.com/blog-feed.xml and https://www.kwmedia.info/blog-feed.xml
# on 2026-11-05 (public feeds), cut down to a few items without descriptions, images or authors.
FEED = {"architecture": (FIXTURES / "wix_architecture_feed.xml").read_text(),
        "media": (FIXTURES / "wix_media_feed.xml").read_text()}
ARCH, MEDIA = wixfeeds.FEEDS
NOW = datetime(2026, 11, 5, 3, 0, tzinfo=UTC)  # 12:00 KST


def feeds(calls=None, pages=None):
    pages = pages or {}

    def fake(url, types=("text/html",)):
        assert types == wixfeeds.TYPES
        feed = next(f for f in wixfeeds.FEEDS if f.url == url)
        if calls is not None:
            calls.append(feed.key)
        return FetchResult(pages.get(feed.key, FEED[feed.key]), None, 200)
    return fake


def notices(table, source):
    return {i["post_id"]: i for i in table.scan()["Items"] if i["PK"] == f"SRC#{source}" and i["SK"].startswith("N#")}


def test_parse_feed_items():
    items = wixfeeds.parse_feed(FEED["architecture"], ARCH)
    assert [r.duid for _, r in items] == ["6aa9fd2272df054056875890", "6a87f3acd26ca05ca3dd15a2", "69a9147a95f896866353d1bb"]
    published, first = items[0]
    assert published == int(datetime(2026, 9, 16, 2, 24, 17, tzinfo=UTC).timestamp())
    assert first.title == "2026학년도 2학기 교수학습센터 기초학력증진프로그램 기초학력 클리닉 튜터링 안내"
    assert first.posted_date.isoformat() == "2026-09-16" and not first.pinned
    assert first.category_id == "dept-architecture" and first.department == "건축학과"
    assert first.source_url.startswith("https://www.kwuarchitecture.com/single-post/")
    media = wixfeeds.parse_feed(FEED["media"], MEDIA)
    assert media[0][1].title.startswith("2026학년도 2학기[선후배의 만남!-미컴 밥약 나잇]")  # leading space trimmed


def test_dates_are_kst_and_bad_items_are_skipped():
    late = FEED["media"].replace("Tue, 27 Oct 2026 07:28:45 GMT", "Tue, 27 Oct 2026 16:30:00 GMT")
    assert wixfeeds.parse_feed(late, MEDIA)[1][1].posted_date.isoformat() == "2026-10-28"  # 01:30 KST
    broken = FEED["media"].replace("<guid isPermaLink=\"false\">6ae0521818163c9dd52fd4e0", "<guid>x")
    assert len(wixfeeds.parse_feed(broken, MEDIA)) == 1
    elsewhere = FEED["media"].replace("https://www.kwmedia.info/post/2026", "https://evil.example/post/2026")
    assert len(wixfeeds.parse_feed(elsewhere, MEDIA)) == 1
    assert wixfeeds.parse_feed("<html><body>점검 중</body></html>", MEDIA) == []
    assert wixfeeds.parse_feed("not xml <", MEDIA) == []
    redacted = FEED["media"].replace("Dean's List 시상식 개최", "시상식 문의 010-1234-5678")
    assert "010-1234-5678" not in wixfeeds.parse_feed(redacted, MEDIA)[1][1].title


def test_fetch_accepts_the_feed_content_type(monkeypatch):
    class Response:
        status, headers = 200, {"Content-Type": "text/xml; charset=UTF-8"}

        done = False

        def read1(self, limit):
            body, self.done = (b"" if self.done else FEED["media"].encode()), True
            return body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    seen = []

    class Opener:
        def open(self, request, timeout):
            seen.append(request.get_header("Accept"))
            return Response()

    monkeypatch.setattr(fetch, "_opener", Opener())
    assert fetch.fetch_list_page(MEDIA.url).block_reason == "markup"  # an HTML page is expected by default
    result = fetch.fetch_list_page(MEDIA.url, types=wixfeeds.TYPES)
    assert result.block_reason is None and wixfeeds.parse_feed(result.html, MEDIA)
    assert seen == ["text/html", "application/rss+xml, application/xml, text/xml"]


def test_seeds_quietly_then_new_by_publish_time(aws):
    table = aws["public"]
    first = wixfeeds.collect(NOW, table, fetch=feeds())
    assert first["architecture"] == {"status": "ok", "rows": 3, "seed_run": True, "inserted": 3, "updated": 0, "unchanged": 0}
    assert first["media"]["seed_run"] is True and all(i["seed"] for i in notices(table, "dept-media").values())

    # A new post on top; an old post the feed shows again under a new id (or after the TTL) stays quiet.
    newer = (FEED["media"].replace("6aea7dbf45f0aaa1eb83fc02", "6aeb0000aaaaaaaaaaaaaaaa")
             .replace("Wed, 04 Nov 2026 00:47:13 GMT", "Thu, 05 Nov 2026 05:00:00 GMT")
             .replace("6ae0521818163c9dd52fd4e0", "6ae0ffffbbbbbbbbbbbbbbbb"))
    second = wixfeeds.collect(NOW + timedelta(hours=2), table, fetch=feeds(pages={"media": newer}))
    assert second["media"]["inserted"] == 2 and second["media"]["seed_run"] is False
    rows = notices(table, "dept-media")
    assert rows["6aeb0000aaaaaaaaaaaaaaaa"]["seed"] is False and rows["6ae0ffffbbbbbbbbbbbbbbbb"]["seed"] is True
    assert rows["6aeb0000aaaaaaaaaaaaaaaa"]["posted_date"] == "2026-11-05"

    def stream(item):
        return {"eventName": "INSERT", "dynamodb": {"NewImage": {
            "PK": {"S": item["PK"]}, "SK": {"S": item["SK"]}, "category_id": {"S": item["category_id"]},
            "seed": {"BOOL": item["seed"]}, "pinned": {"BOOL": item["pinned"]}, "posted_date": {"S": item["posted_date"]}}}}

    jobs = dispatcher.jobs_for([stream(r) for r in rows.values()], NOW.date())
    assert [(j["key"]["SK"], j["channel"]) for j in jobs] == [("N#6aeb0000aaaaaaaaaaaaaaaa", "department")]


def test_each_feed_at_most_every_two_hours_one_request_per_host(aws):
    table, calls = aws["public"], []
    for minutes in range(0, 241, 20):  # every scheduled run for four hours, with a few seconds of jitter
        out = wixfeeds.collect(NOW + timedelta(minutes=minutes, seconds=5 * (minutes % 3)), table, fetch=feeds(calls))
        assert set(out) == {"architecture", "media"}
    assert calls == ["architecture", "media"] * 3  # 12:00, 14:00, 16:00
    origins = {i["PK"] for i in table.scan()["Items"] if i["PK"].startswith("ORIGIN#")}
    assert origins == {"ORIGIN#www.kwuarchitecture.com", "ORIGIN#www.kwmedia.info"}


def test_a_block_cools_down_only_that_host(aws):
    table = aws["public"]

    def fake(url, types=()):
        return FetchResult(None, "rate_limit", 429) if url == ARCH.url else FetchResult(FEED["media"], None, 200)

    out = wixfeeds.collect(NOW, table, fetch=fake)
    assert out["architecture"] == {"status": "blocked", "reason": "rate_limit"} and out["media"]["status"] == "ok"
    assert not politeness.acquire(table, ARCH.host, NOW + timedelta(minutes=25), daily_cap=wixfeeds.DAILY_CAP).granted
    assert politeness.acquire(table, MEDIA.host, NOW + timedelta(minutes=25), daily_cap=wixfeeds.DAILY_CAP).granted


def test_feed_posts_are_department_categories_and_searchable(aws):
    table = aws["public"]
    wixfeeds.collect(NOW, table, fetch=feeds())
    public_data.clear_cache()
    assert routing.CATEGORY_LABEL["dept-architecture"] == "건축학과" and routing.channel_for("dept-media") == "department"
    hits = public_data.search(table, "졸업연기", None, None, 20)
    assert [h["post_id"] for h in hits] == ["dept-architecture-6a87f3acd26ca05ca3dd15a2"]
    assert hits[0]["category"] == hits[0]["department"] == "건축학과"
    assert {r["category"] for r in public_data.latest(table, "미컴", 10)} == {"미디어커뮤니케이션학부"}
    assert public_data.get(table, "dept-media-6aea7dbf45f0aaa1eb83fc02")["posted_date"] == "2026-11-04"


def test_a_future_pub_date_does_not_silence_later_posts(aws, capsys):
    table = aws["public"]
    wixfeeds.collect(NOW, table, fetch=feeds())  # seed
    typo = FEED["media"].replace("6aea7dbf45f0aaa1eb83fc02", "6aeb1111aaaaaaaaaaaaaaaa").replace(
        "Wed, 04 Nov 2026 00:47:13 GMT", "Fri, 05 Nov 2027 00:00:00 GMT")  # a year ahead
    wixfeeds.collect(NOW + timedelta(hours=2), table, fetch=feeds(pages={"media": typo}))
    assert '"wix_future_date"' in capsys.readouterr().out
    state = table.get_item(Key={"PK": "SRC#dept-media", "SK": "STATE"})["Item"]
    assert int(state["max_published"]) == int((NOW + timedelta(hours=2)).timestamp())  # the run's time at most
    real = typo.replace("6ae0521818163c9dd52fd4e0", "6aec2222bbbbbbbbbbbbbbbb").replace(
        "Tue, 27 Oct 2026 07:28:45 GMT", "Fri, 06 Nov 2026 01:00:00 GMT")
    wixfeeds.collect(NOW + timedelta(days=1, hours=2), table, fetch=feeds(pages={"media": real}))
    assert notices(table, "dept-media")["6aec2222bbbbbbbbbbbbbbbb"]["seed"] is False  # announced


def test_a_missing_feed_backs_off_only_that_feed(aws):
    table, calls = aws["public"], []

    def fake(url, types=()):
        calls.append(url)
        return FetchResult(None, "http_failure", 404) if url == ARCH.url else FetchResult(FEED["media"], None, 200)

    for hours in (0, 2, 4, 6):
        wixfeeds.collect(NOW + timedelta(hours=hours), table, fetch=fake)
    assert calls.count(ARCH.url) == 3 and calls.count(MEDIA.url) == 4  # 0 h, 2 h, then 4 h of rest: 6 h
    assert politeness.acquire(table, ARCH.host, NOW + timedelta(hours=6, minutes=2), daily_cap=7).granted
