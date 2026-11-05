"""www.kw.ac.kr 취업게시판 and 종합 FAQ: main-board layout, own categories, checked rarely."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from kwcampus import dispatcher, politeness, public_data, wwwboards
from kwcampus.config import KST
from kwcampus.fetch import FetchResult
from kwcampus.kwnotice.source import NOTICE_HOST

FIXTURES = Path(__file__).parent / "fixtures"
# Saved from https://www.kw.ac.kr/ko/life/job.jsp and /ko/life/faq.jsp on 2026-11-05 (public pages),
# cut down to a few real rows each.
PAGES = {"job": (FIXTURES / "www_job_list.html").read_text(), "faq": (FIXTURES / "www_faq_list.html").read_text()}
JOB, FAQ = wwwboards.BOARDS
NOW = datetime(2026, 11, 5, 3, 0, tzinfo=UTC)  # 12:00 KST


class Clock:
    """Real time as seen by the collector: sleeping moves it forward."""

    def __init__(self, start):
        self.now, self.slept = start, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


def site(calls):
    def fake(url):
        calls.append(url)
        return FetchResult(PAGES["job" if "job.jsp" in url else "faq"], None, 200)
    return fake


def main_board_request(table, at):
    """What the main board does first in every run: take www's permit for this run."""
    assert politeness.acquire(table, NOTICE_HOST, at, daily_cap=wwwboards.DAILY_CAP).granted


def test_job_board_rows_keep_the_main_board_layout():
    rows = wwwboards.parse_board(PAGES["job"], JOB)
    assert [(r.duid, r.pinned) for r in rows] == [
        ("53606", True), ("53592", True), ("53583", False), ("53571", False), ("53507", False)]
    first = rows[0]
    assert first.title == "[대학혁신지원사업] 2026 광운대학교 취업박람회 개최 안내"  # "[취업관련]" and "신규게시글" left out
    assert first.category_id == "job" and first.department == "경력개발팀"
    assert first.posted_date.isoformat() == "2026-11-04"
    assert first.source_url == "https://www.kw.ac.kr/ko/life/job.jsp?BoardMode=view&DUID=53606"
    assert [r.attachments_present for r in rows] == [False, False, False, True, False]


def test_faq_rows_with_team_labels_or_none():
    rows = wwwboards.parse_board(PAGES["faq"], FAQ)
    assert [r.duid for r in rows] == ["53053", "52716", "833", "832"]  # 833 has no "[분류]" at all
    assert {r.category_id for r in rows} == {"faq"} and rows[1].department == "시설안전관리팀"
    assert rows[0].source_url == "https://www.kw.ac.kr/ko/life/faq.jsp?BoardMode=view&DUID=53053"


def test_checked_rarely_after_waiting_out_the_main_boards_gap(aws):
    table, calls = aws["public"], []
    start = NOW
    for minutes, expected in [(0, "job"), (20, "faq"), (40, None), (60, None), (120, "job"), (140, None)]:
        run_at = start + timedelta(minutes=minutes)
        main_board_request(table, run_at)
        clock = Clock(run_at + timedelta(seconds=2))
        before = len(calls)
        out = wwwboards.collect(run_at, table, fetch=site(calls), clock=clock, sleep=clock.sleep)
        if expected is None:
            assert out == {"status": "not_due"} and len(calls) == before and clock.slept == []
        else:
            assert out["status"] == "ok" and out["board"] == f"kw-{expected}" and out["seed_run"] == (minutes < 60)
            assert calls[-1] == f"https://www.kw.ac.kr/ko/life/{expected}.jsp" and len(calls) == before + 1
            assert clock.slept == [politeness.MIN_INTERVAL.total_seconds() + 1]  # never two www requests in a row
    # next day: both are due again, one per run
    for minutes, expected in [(0, "kw-job"), (20, "kw-faq")]:
        run_at = start + timedelta(days=1, minutes=minutes)
        main_board_request(table, run_at)
        clock = Clock(run_at)
        assert wwwboards.collect(run_at, table, fetch=site(calls), clock=clock, sleep=clock.sleep)["board"] == expected


def test_shares_the_www_daily_cap_and_stays_due(aws):
    table, calls = aws["public"], []
    today = NOW.astimezone(KST).strftime("%Y-%m-%d")
    table.put_item(Item={"PK": f"ORIGIN#{NOTICE_HOST}", "SK": "STATE", "day": today,
                         "day_count": wwwboards.DAILY_CAP, "ver": 1})
    clock = Clock(NOW)
    out = wwwboards.collect(NOW, table, fetch=site(calls), clock=clock, sleep=clock.sleep)
    assert out == {"status": "skipped", "board": "kw-job", "reason": "daily_cap"} and calls == [] and clock.slept == []
    tomorrow = NOW + timedelta(days=1)
    clock = Clock(tomorrow)
    assert wwwboards.collect(tomorrow, table, fetch=site(calls), clock=clock, sleep=clock.sleep)["board"] == "kw-job"


def test_a_block_cools_down_www_for_the_main_board_too(aws):
    table = aws["public"]
    clock = Clock(NOW)
    out = wwwboards.collect(NOW, table, fetch=lambda url: FetchResult(None, "forbidden", 403), clock=clock,
                            sleep=clock.sleep)
    assert out == {"status": "blocked", "board": "kw-job", "reason": "forbidden"}
    later = NOW + timedelta(minutes=20)
    assert politeness.acquire(table, NOTICE_HOST, later, daily_cap=40).reason == "cooldown"


def test_job_posts_alert_the_career_channel_and_faq_never_alerts(aws):
    table, calls = aws["public"], []
    for minutes in (0, 20):
        clock = Clock(NOW + timedelta(minutes=minutes))
        wwwboards.collect(NOW + timedelta(minutes=minutes), table, fetch=site(calls), clock=clock, sleep=clock.sleep)

    def insert(category_id):
        return {"eventName": "INSERT", "dynamodb": {"NewImage": {
            "PK": {"S": "SRC#x"}, "SK": {"S": "N#1"}, "category_id": {"S": category_id},
            "seed": {"BOOL": False}, "pinned": {"BOOL": False}, "posted_date": {"S": "2026-11-05"}}}}

    jobs = dispatcher.jobs_for([insert("job"), insert("faq")], NOW.date())
    assert [j["channel"] for j in jobs] == ["career"]

    public_data.clear_cache()
    interview = public_data.search(table, "면접", "취업", None, 20)
    assert {r["post_id"] for r in interview} == {"kw-job-53583", "kw-job-53507"}
    assert {r["category"] for r in interview} == {"취업"} and public_data.category_id("채용") == "job"
    faq = public_data.latest(table, "FAQ", 10)
    assert [r["post_id"] for r in faq] == ["kw-faq-53053", "kw-faq-52716", "kw-faq-833", "kw-faq-832"]
    assert public_data.get(table, "kw-job-53606")["department"] == "경력개발팀"


def test_a_changed_page_does_not_pause_the_main_board(aws):
    table = aws["public"]
    clock = Clock(NOW)
    main_board_request(table, clock())
    out = wwwboards.collect(NOW, table, fetch=lambda url: FetchResult("<html>새 디자인</html>", None, 200),
                            clock=clock, sleep=clock.sleep)
    assert out == {"status": "no_rows", "board": "kw-job"}
    clock.sleep(61)
    assert politeness.acquire(table, NOTICE_HOST, clock(), daily_cap=wwwboards.DAILY_CAP).granted


def test_a_late_run_skips_the_wait_instead_of_running_out_of_time(aws):
    table = aws["public"]
    clock = Clock(NOW)
    main_board_request(table, clock())
    out = wwwboards.collect(NOW, table, fetch=site([]), clock=clock, sleep=clock.sleep, time_left=lambda: 40)
    assert out["reason"] == "no_time" and clock.slept == []


def test_an_old_job_post_coming_back_is_not_announced_again(aws):
    table = aws["public"]
    clock = Clock(NOW)
    wwwboards.collect(NOW, table, fetch=site([]), clock=clock, sleep=clock.sleep)  # seeds
    first = [i for i in table.scan()["Items"] if i["PK"] == "SRC#kw-job" and i["SK"].startswith("N#")]
    victim = first[-1]
    table.delete_item(Key={"PK": victim["PK"], "SK": victim["SK"]})  # as if its TTL ran out
    later = NOW + timedelta(hours=3)
    clock.now = later
    wwwboards.collect(later, table, fetch=site([]), clock=clock, sleep=clock.sleep)
    back = table.get_item(Key={"PK": victim["PK"], "SK": victim["SK"]})["Item"]
    assert back["seed"] is True  # stored quietly, no second Discord alert
