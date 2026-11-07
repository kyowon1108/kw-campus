"""총동문회 (gnuboard), 총학생회 (Next.js flight data) and 현장실습 (robots.txt allows /index.do only):
one page per board on a light schedule, each on its own server gate."""

import asyncio
import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import test_extract
from kw_mcp import notices as kw_notices
from test_extract import PNG, FakeClaude, keys, page_reader

from kwcampus import (
    dispatcher,
    extract,
    gnuboard,
    notice_assets,
    opsdigest,
    politeness,
    public_data,
    routing,
    smallboards,
    store,
)
from kwcampus.bridge import install
from kwcampus.fetch import FetchResult

isolated = test_extract.isolated  # no API key, client or spend ledger kept between tests; read_notice offline

FIXTURES = Path(__file__).parent / "fixtures"
# Saved on 2026-11-07 (public pages): the 총동문회 notice list (its rows only; the writer is the office) and
# the 2026 장학생 선발 post (the article only, the download nonce replaced); the 총학생회 notices page cut to 8
# posts with their text and contact address blanked; the 현장실습 home page's 공지사항 box.
ALUMNI_LIST = (FIXTURES / "gnuboard_kwuaa_notice_list.html").read_text()
ALUMNI_POST = (FIXTURES / "gnuboard_kwuaa_notice_86.html").read_text()
COUNCIL_PAGE = (FIXTURES / "council_notices.html").read_text()
INTERNSHIP_PAGE = (FIXTURES / "internship_index.html").read_text()
PAGES = {smallboards.ALUMNI.list_url: ALUMNI_LIST, smallboards.COUNCIL.list_url: COUNCIL_PAGE,
         smallboards.INTERNSHIP.list_url: INTERNSHIP_PAGE}
ALUMNI, COUNCIL, INTERNSHIP = smallboards.ALUMNI, smallboards.COUNCIL, smallboards.INTERNSHIP
NOW = datetime(2026, 11, 9, 0, 0, 5, tzinfo=UTC)  # Monday 09:00 KST, the first run of the day
TODAY = date(2026, 11, 9)
POST_86 = "https://www.kwuaa.com/bbs/board.php?bo_table=notice&wr_id=86"


def council_with(edit) -> str:
    """The council page with its flight data text changed by edit(text)."""
    push = re.search(r"self\.__next_f\.push\((\[1,.*?\])\)</script>", COUNCIL_PAGE, re.DOTALL).group(1)
    return COUNCIL_PAGE.replace(push, json.dumps([1, edit(json.loads(push)[1])], ensure_ascii=False))


def site(calls=None, pages=None):
    pages = {**PAGES, **(pages or {})}

    def fetch(url):
        if calls is not None:
            calls.append(url)
        page = pages[url]
        return page if isinstance(page, FetchResult) else FetchResult(page, None, 200)
    return fetch


def notices(table, source):
    return {i["post_id"]: i for i in table.scan()["Items"] if i["PK"] == f"SRC#{source}" and i["SK"].startswith("N#")}


def stream(item):
    return {"eventName": "INSERT", "dynamodb": {"NewImage": {
        "PK": {"S": item["PK"]}, "SK": {"S": item["SK"]}, "category_id": {"S": item["category_id"]},
        "seed": {"BOOL": item["seed"]}, "pinned": {"BOOL": item["pinned"]}, "posted_date": {"S": item["posted_date"]}}}}


# --- 총동문회: gnuboard rows and their years ---------------------------------------------------------------

def test_alumni_rows_get_their_year_from_the_row_order():
    rows = smallboards.parse(ALUMNI_LIST, ALUMNI, TODAY)
    assert [int(r.duid) for r in rows] == [88, 87, 86, 85, 84, 83, 82, 81, 80, 79, 78, 75, 74, 73, 72, 71]
    dated = {int(r.duid): r.posted_date.isoformat() for r in rows}
    # "10-27" ... "01-19" are 2026; "12-19" under "01-19" jumps forward, so it and the rest are 2025
    assert dated[88] == "2026-10-27" and dated[81] == "2026-01-19" and dated[80] == "2025-12-19"
    assert dated[86] == "2026-08-28" and dated[73] == "2025-08-21" and dated[71] == "2025-07-16"  # the 장학 calls
    first = rows[2]
    assert first.title == "2026년도 광운대학교 총동문회 장학생 선발 안내" and first.attachments_present
    assert str(first.source_url) == POST_86
    assert first.category_id == "office-alumni" and first.department == first.category_name == "총동문회"
    assert not any(r.pinned for r in rows) and [r.attachments_present for r in rows].count(True) == 4


def test_a_list_read_early_in_january_puts_december_in_the_year_before():
    rows = smallboards.parse(ALUMNI_LIST, ALUMNI, date(2027, 1, 3))
    assert rows[0].posted_date == date(2026, 10, 27) and rows[-1].posted_date == date(2025, 7, 16)
    # a list read before 10-27 of its year: that row can't be from the future, so it is last year's
    assert smallboards.parse(ALUMNI_LIST, ALUMNI, date(2026, 10, 1))[0].posted_date == date(2025, 10, 27)


GNU_BASIC = """<table><tbody>
<tr class="bo_notice"><td class="td_num2"><strong class="notice_icon">공지</strong></td>
<td class="td_subject"><div class="bo_tit"><a href="./board.php?bo_table=free&amp;wr_id=3">이용 안내</a></div></td>
<td class="td_datetime">24-03-02</td></tr>
<tr><td class="td_num2">12</td><td class="td_subject"><div class="bo_tit">
<a href="https://club.example.ac.kr/bbs/board.php?bo_table=free&amp;wr_id=40">오늘 글</a>
<a href="https://club.example.ac.kr/bbs/board.php?bo_table=free&amp;wr_id=40#c_41"><span class="cnt_cmt">2</span></a>
<i class="fa fa-download"></i></div></td><td class="td_datetime">09:14</td></tr>
<tr><td class="td_num2">11</td><td class="td_subject"><a href="/bbs/board.php?bo_table=free&amp;wr_id=31">2월 29일 글</a></td>
<td class="td_datetime">02-29</td></tr>
<tr><td class="td_num2">10</td><td class="td_subject"><a href="/bbs/board.php?bo_table=other&amp;wr_id=30">다른 게시판</a></td>
<td class="td_datetime">01-02</td></tr>
<tr><td class="td_num2">9</td><td class="td_subject"><a href="https://elsewhere.example/bbs/board.php?bo_table=free&amp;wr_id=29">다른 사이트</a></td>
<td class="td_datetime">01-01</td></tr>
</tbody></table>"""


def test_the_parser_reads_the_basic_skin_of_any_gnuboard_site():
    rows = gnuboard.parse_list(GNU_BASIC, "club.example.ac.kr", "free", date(2026, 11, 9))
    assert [(r.wr_id, r.title, r.posted.isoformat(), r.pinned, r.files) for r in rows] == [
        (40, "오늘 글", "2026-11-09", False, True),  # "HH:MM" is today; the comment link is the same row
        (31, "2월 29일 글", "2024-02-29", False, False),  # the last leap year on or before the row above
        (3, "이용 안내", "2024-03-02", True, False),  # pinned, with its own year, placed by wr_id
    ]
    assert gnuboard.post_url("club.example.ac.kr", "free", 40) == "https://club.example.ac.kr/bbs/board.php?bo_table=free&wr_id=40"


def test_alumni_titles_are_masked_and_broken_pages_give_nothing():
    masked = ALUMNI_LIST.replace("2026년도 제1차 임시이사회 개최 안내", "임시이사회 문의 010-1234-5678")
    assert "010-1234-5678" not in smallboards.parse(masked, ALUMNI, TODAY)[1].title
    assert smallboards.parse("<html><body>점검 중</body></html>", ALUMNI, TODAY) == []
    no_dates = re.sub(r'<div class="e_date"[^>]*>.*?</div>', "", ALUMNI_LIST)
    assert smallboards.parse(no_dates, ALUMNI, TODAY) == []


# --- 총학생회: the flight data -----------------------------------------------------------------------------

def test_council_posts_come_from_the_flight_data():
    rows = smallboards.parse(COUNCIL_PAGE, COUNCIL, TODAY)
    assert len(rows) == 8 and len({r.duid for r in rows}) == 8
    top = rows[0]
    assert (top.duid, top.title, top.posted_date.isoformat(), top.pinned) == (
        "cl1789744612064dlre8hp", "KW해커톤 참가자 모집", "2026-09-09", True)
    assert str(top.source_url) == "https://www.kwu-studentcouncil.co.kr/notices/cl1789744612064dlre8hp"
    assert top.category_id == "office-council" and top.department == "총학생회"
    by_id = {r.duid: r for r in rows}
    assert by_id["cl1766766399496jfwp51u"].posted_date == date(2025, 12, 22)  # publishedAt, not createdAt (12-26)
    assert by_id["cl17672086038288upy1pl"].attachments_present  # two Google Drive files
    assert sum(r.pinned for r in rows) == 2


def test_council_flight_data_split_over_two_pushes_and_broken_pages():
    whole = [r.duid for r in smallboards.parse(COUNCIL_PAGE, COUNCIL, TODAY)]
    push = re.search(r"self\.__next_f\.push\((\[1,.*?\])\)</script>", COUNCIL_PAGE, re.DOTALL).group(1)
    text = json.loads(push)[1]
    half = text.index("cl1789744604578n92et29")  # a row cut in the middle of a post
    two = "".join(f"<script>self.__next_f.push({json.dumps([1, part], ensure_ascii=False)})</script>"
                  for part in (text[:half], text[half:]))
    page = COUNCIL_PAGE.replace(f"<script>self.__next_f.push({push})</script>", two)
    assert page.count("self.__next_f.push([1,") == 2
    assert [r.duid for r in smallboards.parse(page, COUNCIL, TODAY)] == whole
    broken = council_with(lambda t: t.replace('"notices":[', '"notices":[{"id":"x"},{"title":"제목만"},'))
    assert [r.duid for r in smallboards.parse(broken, COUNCIL, TODAY)] == whole  # malformed posts are skipped
    assert smallboards.parse("<html><body>공지사항</body></html>", COUNCIL, TODAY) == []
    renamed = council_with(lambda t: t.replace("pinnedNotices", "pinned").replace('"notices"', '"posts"'))
    assert smallboards.parse(renamed, COUNCIL, TODAY) == []


def test_council_keeps_no_text_thumbnail_or_contact():
    rows = smallboards.parse(COUNCIL_PAGE.replace("(본문 생략)", "문의 010-9999-8888"), COUNCIL, TODAY)
    item = store.notice_item(COUNCIL.source, rows[0], NOW, seed=True)
    assert "010-9999-8888" not in str(item) and "supabase" not in str(item) and "instagram" not in str(item)


# --- 현장실습: the home page widget only ------------------------------------------------------------------

def test_internship_widget_rows():
    rows = smallboards.parse(INTERNSHIP_PAGE, INTERNSHIP, TODAY)
    assert [r.duid for r in rows][:3] == ["689", "688", "687"] and len(rows) == 13
    assert rows[0].title == "인턴 채용 안내(네오팜/한국휴렛팩커드/매드업/SNOW/IBK기업은행)"  # the [전체] label goes
    assert rows[0].posted_date == date(2026, 11, 2) and rows[0].category_id == "office-internship"
    assert str(rows[0].source_url) == "https://internship.kw.ac.kr/home/notice/view.do?bbsSeq=689"
    assert rows[5].title.startswith("[현장실습] 「가상기업(광운그룹)")
    off_site = INTERNSHIP_PAGE.replace('/home/notice/view.do?bbsSeq=689&page=1', 'https://evil.example/view.do?bbsSeq=1')
    assert smallboards.parse(off_site, INTERNSHIP, TODAY)[0].duid == "688"  # a link off the site is skipped


def test_internship_robots_allows_the_home_page_only():
    allowed = politeness.robots_allows
    assert allowed("https://internship.kw.ac.kr/index.do") and allowed(INTERNSHIP.list_url)
    for path in ("/", "/home/notice/view.do?bbsSeq=689", "/home/notice/index.do", "/download.do?fileSn=1",
                 "/index.do/../home/notice/view.do", "/index.do/%2e%2e/download.do"):
        assert not allowed(f"https://internship.kw.ac.kr{path}"), path
    # the other two boards' hosts have no robots.txt (404); the dorm's host was never recorded: fail closed
    assert allowed(ALUMNI.list_url) and allowed(POST_86) and allowed(COUNCIL.list_url)
    assert not allowed("https://kw.happydorm.or.kr/60/6010.do")


def test_internship_notices_are_never_fetched_by_any_reader(aws):
    """The list is the only request: no page run, no image or file, no connector read of its notices."""
    table = aws["public"]
    smallboards.collect(NOW, table, fetch=site())  # seed
    newer = INTERNSHIP_PAGE.replace("bbsSeq=689&", "bbsSeq=690&").replace("2026-11-02", "2026-11-08", 1)
    calls = []
    out = smallboards.collect(NOW + timedelta(hours=4), table, fetch=site(calls, {INTERNSHIP.list_url: newer}))
    assert out["internship"]["inserted"] == 1 and [c for c in calls if INTERNSHIP.host in c] == [INTERNSHIP.list_url]
    fresh = notices(table, "internship")["690"]
    assert fresh["seed"] is False and fresh["url"] == "https://internship.kw.ac.kr/home/notice/view.do?bbsSeq=690"
    assert extract.page_for(fresh) is None and not extract.wanted(fresh)
    assert extract.page_for_host(INTERNSHIP.host) is None
    keys(aws)
    fetched = []
    assert extract.collect(NOW + timedelta(hours=4, minutes=10), table, fetch=lambda url, **kw: fetched.append(url),
                           anthropic_client=FakeClaude()) == {"status": "ok", "title_only": 1}
    assert fetched == [] and not test_extract.queue(table)
    read = notices(table, "internship")["690"]["read"]
    assert (read["status"], read["body"], read["last_reason"], read["tries"]) == ("title_only", "none", "title_only_robots", 0)
    assert public_data.public_view(notices(table, "internship")["690"])["read"] == {"status": "title_only", "missing": ["body"]}

    async def try_asset(url):
        await notice_assets.PoliteGate(table, lambda: NOW, refused=kw_notices.Refused).before(url)

    for url in (fresh["url"], INTERNSHIP.list_url, "https://internship.kw.ac.kr/download.do?fileSn=1"):
        with pytest.raises(kw_notices.Refused):
            asyncio.run(try_asset(url))
        with pytest.raises(kw_notices.NoticeError):
            kw_notices.check_url(url, asset=True)


# --- the collector -----------------------------------------------------------------------------------------

def test_seeds_quietly_then_announces_new_posts_to_their_channels(aws):
    table = aws["public"]
    first = smallboards.collect(NOW, table, fetch=site())
    assert first == {
        "alumni": {"status": "ok", "rows": 16, "seed_run": True, "inserted": 16, "updated": 0, "unchanged": 0,
                   "twins": 0},
        "council": {"status": "ok", "rows": 8, "seed_run": True, "inserted": 8, "updated": 0, "unchanged": 0,
                    "twins": 0},
        "internship": {"status": "ok", "rows": 13, "seed_run": True, "inserted": 13, "updated": 0, "unchanged": 0,
                       "twins": 0}}
    assert all(i["seed"] for b in smallboards.BOARDS for i in notices(table, b.source).values())
    assert not test_extract.queue(table)  # seed rows are never read

    alumni = ALUMNI_LIST.replace("wr_id=88", "wr_id=90").replace("10-27", "11-06", 1)

    def new_post(text):  # 추석 귀향 버스 becomes a new post of 11-06
        at = text.index('"id":"cl1789744612718sbnrjp7"')
        head, tail = text[:at], text[at:]
        return head + tail.replace("cl1789744612718sbnrjp7", "cl1799000000000newpost1", 1).replace(
            '"publishedAt":"2026-09-09T00:00:00"', '"publishedAt":"2026-11-06T00:00:00"', 1)

    council = council_with(new_post)
    later = NOW + timedelta(hours=4)
    second = smallboards.collect(later, table, fetch=site(pages={ALUMNI.list_url: alumni, COUNCIL.list_url: council}))
    assert second["alumni"]["inserted"] == 1 and second["council"]["inserted"] == 1
    new = [notices(table, "alumni")["90"], notices(table, "council")["cl1799000000000newpost1"]]
    assert [n["seed"] for n in new] == [False, False] and new[0]["posted_date"] == "2026-11-06"
    jobs = dispatcher.jobs_for([stream(i) for b in smallboards.BOARDS for i in notices(table, b.source).values()],
                               later.date())
    assert sorted((j["key"]["PK"], j["channel"]) for j in jobs) == [("SRC#alumni", "department"),
                                                                    ("SRC#council", "notice")]
    queued = {row["notice_pk"]: row for row in test_extract.queue(table)}
    assert set(queued) == {"SRC#alumni", "SRC#council"}  # queued like every new notice, and both are read
    assert extract.wanted(new[0]) and extract.wanted(new[1])
    council_row = queued["SRC#council"]  # 총학생회's text rides on its queue row (masked), with its poster's address
    assert council_row["text"] == "(본문 생략)" and council_row["thumb"].startswith("https://lqptfesfsgpdjlzwxhbt.supabase.co/")
    assert "text" not in queued["SRC#alumni"]


def test_an_old_post_turning_up_is_stored_quietly(aws):
    table = aws["public"]
    smallboards.collect(NOW, table, fetch=site())
    older = ALUMNI_LIST.replace("wr_id=71", "wr_id=70")  # a lower number, a year-old date: back after the TTL, say
    smallboards.collect(NOW + timedelta(hours=4), table, fetch=site(pages={ALUMNI.list_url: older}))
    assert notices(table, "alumni")["70"]["seed"] is True


def test_light_schedule_one_request_per_board_on_its_own_gate(aws):
    table, calls = aws["public"], []
    for minutes in range(0, 12 * 60 + 1, 20):  # every run from 09:00 to 21:00, with a few seconds of jitter
        out = smallboards.collect(NOW + timedelta(minutes=minutes, seconds=5 * (minutes % 3)), table, fetch=site(calls))
        assert set(out) == {"alumni", "council", "internship"}
    assert calls.count(ALUMNI.list_url) == calls.count(INTERNSHIP.list_url) == 4  # 09:00, 13:00, 17:00, 21:00
    assert calls.count(COUNCIL.list_url) == 7  # every 2 hours
    origins = {i["PK"] for i in table.scan()["Items"] if i["PK"].startswith("ORIGIN#")}
    assert origins == {f"ORIGIN#{b.server}" for b in smallboards.BOARDS}
    assert all(int(politeness.state(table, b.server)["day_count"]) <= b.daily_cap for b in smallboards.BOARDS)


def test_a_block_cools_down_only_that_server_and_a_404_only_that_board(aws):
    table, calls = aws["public"], []
    blocked = site(calls, {ALUMNI.list_url: FetchResult(None, "forbidden", 403),
                           COUNCIL.list_url: FetchResult(None, "http_failure", 404)})
    out = smallboards.collect(NOW, table, fetch=blocked)
    assert out["alumni"] == {"status": "blocked", "reason": "forbidden"} and out["internship"]["status"] == "ok"
    assert out["council"] == {"status": "failed", "reason": "http_failure"}
    assert not politeness.acquire(table, ALUMNI.server, NOW + timedelta(minutes=25), daily_cap=ALUMNI.daily_cap).granted
    assert politeness.acquire(table, COUNCIL.server, NOW + timedelta(minutes=25), daily_cap=COUNCIL.daily_cap).granted
    # the council board waits 2 h after its 404, then 4 h after the next one
    calls.clear()
    for hours in (2, 4, 6, 8):
        smallboards.collect(NOW + timedelta(hours=hours), table, fetch=blocked)
    assert calls.count(COUNCIL.list_url) == 2


def test_an_empty_page_backs_the_server_off_and_a_challenge_is_a_block(aws):
    table = aws["public"]
    empty = site(pages={COUNCIL.list_url: "<html><body>점검 중</body></html>",
                        INTERNSHIP.list_url: '<html><head><title>Just a moment...</title></head></html>'})
    out = smallboards.collect(NOW, table, fetch=empty)
    assert out["council"] == {"status": "no_rows", "reason": "no_rows"}
    assert out["internship"] == {"status": "blocked", "reason": "captcha"}
    for board in (COUNCIL, INTERNSHIP):
        assert not politeness.acquire(table, board.server, NOW + timedelta(minutes=25), daily_cap=9).granted


def test_robots_and_time_left_are_checked_before_any_request(aws, monkeypatch):
    table, calls = aws["public"], []
    monkeypatch.setitem(politeness.ROBOTS, ALUMNI.host, politeness.RobotsCheck("2026-11-07", "Disallow: /bbs/", ("/bbs/",)))
    out = smallboards.collect(NOW, table, fetch=site(calls), time_left=lambda: 10)
    assert out["alumni"] == {"status": "skipped", "reason": "robots"}
    assert out["council"] == out["internship"] == {"status": "skipped", "reason": "no_time"} and calls == []


# --- 총동문회 notices are read for extraction: body and poster images, never its files --------------------------

def test_alumni_pages_get_a_department_boards_reader_without_files():
    page, url = extract.page_for({"source": "alumni", "url": POST_86})
    assert (page.host, page.origin, page.daily_cap) == (ALUMNI.host, ALUMNI.server, 12)
    assert (page.selector, page.files, page.check_every) == ("#bo_v_img, #bo_v_con", False, ALUMNI.every)
    assert extract.page_for_host(ALUMNI.host) is page and url == POST_86
    assert extract.page_for({"source": "alumni", "url": POST_86.replace("https:", "http:")}) is None
    assert extract.page_for({"source": "council", "url": "https://www.kwu-studentcouncil.co.kr/notices/x"}) is None
    # the body only, not the file list; the poster's alt text is just its file name, so no words at all
    assert extract.body_text(ALUMNI_POST, page.selector) == ""
    attached = ALUMNI_POST.replace('<div id="bo_v_img">\n</div>', '<div id="bo_v_img"><img src="/data/file/notice/a.jpg" '
                                   'alt="2026 장학생 모집 포스터"></div>')
    assert attached != ALUMNI_POST
    assert extract.body_text(attached + "<p>x</p>", page.selector) == "2026 장학생 모집 포스터"
    assert extract.body_text('<div class="view_con"><img alt="포스터: 장학 안내"><img alt="IMG_0001.JPG"></div>',
                             ".view_con") == "포스터: 장학 안내"


def test_the_alumni_server_keeps_only_its_own_list_checks_in_reserve(aws):
    page, _ = extract.page_for({"source": "alumni", "url": POST_86})
    table = aws["public"]
    at = datetime(2026, 11, 5, 0, 10, tzinfo=UTC)  # Thursday 09:10 KST, after the 09:00 check
    politeness.acquire(table, ALUMNI.server, at - timedelta(minutes=10), daily_cap=ALUMNI.daily_cap)
    assert extract.check_slots(ALUMNI.every) == ((9, 0), (13, 0), (17, 0), (21, 0))
    assert extract.spare(table, page, at) == 12 - 1 - 3  # 13:00, 17:00 and 21:00 still to come
    assert extract.spare(table, page, at + timedelta(hours=2)) == 12 - 1 - 3  # 11:10: the same three
    assert extract.spare(table, page, at + timedelta(hours=6)) == 12 - 1 - 2  # 15:10: 17:00 and 21:00
    assert extract.spare(table, page, at + timedelta(hours=8)) == 12 - 1 - 1  # 17:10: 21:00 only
    assert extract.spare(table, page, at + timedelta(hours=12, minutes=5)) == 12 - 1  # 21:15: none


def test_kw_mcp_reads_the_alumni_post_title_date_and_poster_but_not_its_files():
    """gnuboard's download.php serves a file only to the session that viewed the post: never listed, never asked."""
    parsed = kw_notices.parse_page(ALUMNI_POST, POST_86)
    assert parsed.title == "2026년도 광운대학교 총동문회 장학생 선발 안내" and parsed.date == "2026-08-28"
    assert parsed.department == "총동문회"
    assert parsed.attachments == [] and parsed.site_files == 2
    for no in (0, 1):
        with pytest.raises(kw_notices.NoticeError):
            kw_notices.check_url(f"https://www.kwuaa.com/bbs/download.php?bo_table=notice&wr_id=86&no={no}&nonce=NONCE",
                                 asset=True)
    assert [u for u, _ in parsed.images] == [
        "https://www.kwuaa.com/data/editor/2608/thumb-2ff575866c189d75c8e21d9ea9d579ca_1787893581_0391_835x1181.jpg"]
    assert kw_notices.url_for_post_id("alumni-86") == POST_86
    with pytest.raises(kw_notices.NoticeError):  # pages there: the board only
        kw_notices.check_url("https://www.kwuaa.com/bbs/login.php")


def test_a_new_alumni_post_is_read_with_its_poster_and_never_its_files(aws):
    table = aws["public"]
    smallboards.collect(NOW, table, fetch=site())  # seed
    newer = ALUMNI_LIST.replace("wr_id=88", "wr_id=89").replace("10-27", "11-06", 1)
    smallboards.collect(NOW + timedelta(hours=4), table, fetch=site(pages={ALUMNI.list_url: newer}))
    keys(aws)
    post = "https://www.kwuaa.com/bbs/board.php?bo_table=notice&wr_id=89"
    log, claude, calls = [], FakeClaude(), []
    clock, slept = [datetime(2026, 11, 14, 1, 10, tzinfo=UTC)], []  # Saturday: no list run left, the whole cap

    async def sleep(seconds):
        slept.append(seconds)
        clock[0] += timedelta(seconds=seconds)

    out = extract.collect(clock[0], table, fetch=test_extract.fetcher(calls, {post: ALUMNI_POST}), clock=lambda: clock[0],
                          anthropic_client=claude, sleep=sleep,
                          read=page_reader(log, pictures=[PNG], files=[("신청서식.hwp", "신청 기간: 2026년 11월 20일까지")]))
    assert out == {"status": "ok", "extracted": 1} and calls == [post]
    assert log == [(None, False), (None, True)]  # the page (in hand), then with its poster; no attachment=n
    stored = notices(table, "alumni")["89"]
    assert stored["extracted"]["sources_used"] == ["images"]  # a poster-only post: its poster is the notice
    assert [b["type"] for b in claude.calls[0]["messages"][0]["content"]].count("image") == 1
    state = politeness.state(table, ALUMNI.server)
    assert int(state["day_count"]) == 2  # the page and the poster, both on 총동문회's own gate; no file request
    assert slept and min(slept) >= politeness.ASSET_GAP_S  # a few seconds apart, like a browser opening the post


# --- what readers and operators see ------------------------------------------------------------------------

def test_the_boards_are_found_by_their_names(aws):
    table = aws["public"]
    smallboards.collect(NOW, table, fetch=site())
    public_data.clear_cache()
    for name, cid in (("총동문회", "office-alumni"), ("총학생회", "office-council"), ("현장실습", "office-internship"),
                      ("동문회", "office-alumni"), ("총학", "office-council"), ("학생회", "office-council")):
        assert public_data.resolve_category(name) == frozenset({cid}), name
    assert {"총동문회", "총학생회", "현장실습"} <= set(public_data.valid_labels())
    hits = public_data.search(table, "장학생 선발", "총동문회", None, 20)
    assert [h["post_id"] for h in hits] == ["alumni-86", "alumni-73"]
    assert hits[0]["category"] == hits[0]["department"] == "총동문회" and hits[0]["url"] == POST_86
    assert [h["post_id"] for h in public_data.search(table, "총동문회", None, None, 50)][:1] == ["alumni-88"]
    assert {r["category"] for r in public_data.latest(table, "총학생회", 50)} == {"총학생회"}
    assert public_data.latest(table, "현장실습", 1)[0]["post_id"] == "internship-689"
    assert public_data.get(table, "council-cl1789744612064dlre8hp")["pinned"] is True
    counts = {c["category"]: c["count"] for c in public_data.categories(table)}
    assert (counts["총동문회"], counts["총학생회"], counts["현장실습"]) == (16, 8, 13)
    assert routing.CATEGORY_LABEL["office-internship"] == "현장실습"


def test_operators_watch_the_new_servers_and_webmasters_can_find_us():
    assert {b.server for b in smallboards.BOARDS} <= set(opsdigest.origins())
    assert opsdigest.STALE_DAYS["alumni"] == 90 and opsdigest.STALE_DAYS["council"] == 60
    collector = install.privacy_page().split("<b>수집하는 곳과 방식</b>")[1].split("</p>")[0]
    for needed in ("총동문회(www.kwuaa.com)", "총학생회(www.kwu-studentcouncil.co.kr)", "현장실습(internship.kw.ac.kr)",
                   "robots.txt가 허용하는 첫 화면(/index.do)만", f"서버마다 {ALUMNI.daily_cap}번", "2~4시간마다"):
        assert needed in collector, needed


# --- review fixes: broken links, labels, zones, re-issued ids, drifting years, errors -------------------------

def test_a_broken_link_is_no_row():
    broken = GNU_BASIC.replace("</tbody>", '<tr><td class="td_num2">13</td><td class="td_subject">'
                               '<a href="http://[club/bbs/board.php?bo_table=free&amp;wr_id=50">깨진 링크</a></td>'
                               '<td class="td_datetime">11-01</td></tr></tbody>')
    assert [r.wr_id for r in gnuboard.parse_list(broken, "club.example.ac.kr", "free", date(2026, 11, 9))] == [40, 31, 3]


def test_internship_drops_only_the_leading_label():
    page = INTERNSHIP_PAGE.replace(
        "<span><strong>[전체]</strong></span>인턴 채용 안내(네오팜/", '<span><strong>[전체]</strong></span>인턴 채용 안내(<span>[신규]</span> 네오팜/', 1
    ).replace("<span><strong>[전체]</strong></span>인턴 채용 안내(가비아/", "<span>마감</span> 인턴 채용 안내(가비아/", 1)
    rows = smallboards.parse(page, INTERNSHIP, TODAY)
    assert rows[0].title.startswith("인턴 채용 안내(") and "[신규] 네오팜" in rows[0].title  # an inner label stays
    assert rows[1].title.startswith("마감 인턴 채용 안내(가비아")  # not a [label]: kept


def test_council_times_with_a_zone_are_kst_dates():
    def zoned(text):
        return (text.replace('"publishedAt":"2026-09-09T00:00:00"', '"publishedAt":"2026-09-08T16:30:00Z"', 1)
                .replace('"publishedAt":"2026-09-03T00:00:00"', '"publishedAt":"2026-09-03T14:59:00+00:00"', 1))
    rows = {r.duid: r.posted_date.isoformat() for r in smallboards.parse(council_with(zoned), COUNCIL, TODAY)}
    assert rows["cl1789744612064dlre8hp"] == "2026-09-09" and rows["cl1789744611401mt8k13j"] == "2026-09-03"
    assert smallboards._day("$D2026-11-01T15:00:00.000Z") == date(2026, 11, 2)


def test_a_reissued_council_id_is_stored_quietly(aws):
    """총학생회's site re-issued its ids once (every 2026 post has an id minted on 2026-09-18). A post under a new
    id with a stored post's title and date is the same post: stored, but never announced."""
    table = aws["public"]
    smallboards.collect(NOW, table, fetch=site())
    recent = council_with(lambda t: t.replace('"publishedAt":"2026-09-09T00:00:00"', '"publishedAt":"2026-11-08T00:00:00"'))
    smallboards.collect(NOW + timedelta(hours=2), table, fetch=site(pages={COUNCIL.list_url: recent}))  # two dated 11-08
    reissued = recent.replace("cl1789744612718sbnrjp7", "cl1799111111111reissue1")
    out = smallboards.collect(NOW + timedelta(hours=4), table, fetch=site(pages={COUNCIL.list_url: reissued}))
    assert out["council"]["inserted"] == 1 and out["council"]["twins"] == 1
    twin = notices(table, "council")["cl1799111111111reissue1"]
    assert twin["seed"] is True and twin["posted_date"] == "2026-11-08"  # recent, yet quiet
    assert dispatcher.jobs_for([stream(twin)], (NOW + timedelta(hours=4)).date()) == []


def test_a_run_of_mostly_new_ids_is_a_seed_run(aws, capsys):
    table = aws["public"]
    smallboards.collect(NOW, table, fetch=site())
    fresh = council_with(lambda t: re.sub(r'"id":"cl(\d+)', r'"id":"cx\1', t).replace(
        '"publishedAt":"2026-09-09T00:00:00"', '"publishedAt":"2026-11-08T00:00:00"'))
    out = smallboards.collect(NOW + timedelta(hours=2), table, fetch=site(pages={COUNCIL.list_url: fresh}))
    assert out["council"]["inserted"] == 8
    rows = {k: v for k, v in notices(table, "council").items() if k.startswith("cx")}
    assert len(rows) == 8 and all(r["seed"] for r in rows.values())  # even the two dated yesterday: quiet
    assert '"small_ids_churned"' in capsys.readouterr().out
    # one new id among many known ones is no churn: announced
    one = fresh.replace("cx1789744612718sbnrjp7", "cl1799000000000newpost1").replace(
        "추석 귀향 버스 신청 (9/23 운행)", "동계 귀향 버스 신청")
    out = smallboards.collect(NOW + timedelta(hours=4), table, fetch=site(pages={COUNCIL.list_url: one}))
    assert out["council"]["inserted"] == 1 and notices(table, "council")["cl1799000000000newpost1"]["seed"] is False


def test_stored_alumni_rows_keep_their_dates(aws):
    """A year guessed one way when first read stays as stored (or as the page run corrected it): no update, no
    re-read, no edit note when a later page reads the same rows another way."""
    table = aws["public"]
    smallboards.collect(NOW, table, fetch=site())
    first = next(r for r in smallboards.parse(ALUMNI_LIST, ALUMNI, TODAY) if r.duid == "86")
    from dataclasses import replace

    table.put_item(Item=store.notice_item("alumni", replace(first, posted_date=date(2025, 8, 28),  # as first read
                                                            updated_date=date(2025, 8, 28)), NOW, seed=True))
    assert notices(table, "alumni")["86"]["posted_date"] == "2025-08-28"
    out = smallboards.collect(NOW + timedelta(hours=4), table, fetch=site())
    assert out["alumni"] == {"status": "ok", "rows": 16, "seed_run": False, "inserted": 0, "updated": 0,
                             "unchanged": 16, "twins": 0}
    assert notices(table, "alumni")["86"]["posted_date"] == "2025-08-28" and not test_extract.queue(table)


def test_the_page_run_corrects_a_guessed_year_without_a_discord_job(aws):
    table = aws["public"]
    smallboards.collect(NOW, table, fetch=site())  # seed
    newer = ALUMNI_LIST.replace("wr_id=88", "wr_id=89").replace("10-27", "11-06", 1)
    smallboards.collect(NOW + timedelta(hours=4), table, fetch=site(pages={ALUMNI.list_url: newer}))
    before = notices(table, "alumni")["89"]
    assert before["posted_date"] == "2026-11-06"
    keys(aws)
    post = "https://www.kwuaa.com/bbs/board.php?bo_table=notice&wr_id=89"
    clock = [datetime(2026, 11, 14, 1, 10, tzinfo=UTC)]

    async def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)

    out = extract.collect(clock[0], table, fetch=test_extract.fetcher([], {post: ALUMNI_POST}), clock=lambda: clock[0],
                          anthropic_client=FakeClaude(), sleep=sleep, read=page_reader([], pictures=[PNG]))
    assert out == {"status": "ok", "extracted": 1}
    after = notices(table, "alumni")["89"]
    assert after["posted_date"] == after["updated_date"] == "2026-08-28"  # the post says 26-08-28 14:06
    assert after["content_hash"] == store.item_hash(after) != before["content_hash"]
    old, new = dict(before), {k: v for k, v in after.items() if k not in ("extracted", "extract_v", "extracted_at",
                                                                           "model", "body_hash", "read")}
    [job] = dispatcher.jobs_for([test_extract.modify(old, new)], clock[0].date())
    assert job["action"] == "edit" and job.get("quiet") is True  # the message follows, with no 수정됨 note
    # the next list run keeps the corrected dates: no update, nothing queued again
    out = smallboards.collect(clock[0] + timedelta(hours=4), table, fetch=site(pages={ALUMNI.list_url: newer}))
    assert out["alumni"]["status"] == "ok" and out["alumni"]["updated"] == 0 and not test_extract.queue(table)
    assert notices(table, "alumni")["89"]["posted_date"] == "2026-08-28"


def test_a_board_that_raises_is_counted(aws, capsys):
    def broken(url):
        raise RuntimeError("parser bug")

    out = smallboards.collect(NOW, aws["public"], fetch=broken)
    assert out == {b.key: {"status": "error"} for b in smallboards.BOARDS}
    logged = capsys.readouterr().out
    assert logged.count('"small_error"') == 3 and '"SmallBoardError": 1' in logged


def test_no_test_reaches_the_network(no_network):
    """conftest refuses every socket connect and the collectors' opener, and fails a test that tried at teardown,
    even when the code under test swallowed the error (smallboards.collect catches everything per board)."""
    import socket

    with pytest.raises(OSError):
        socket.create_connection(("192.0.2.1", 80), timeout=1)
    swallowed = []
    try:  # code that swallows every error, as a collector's per-board guard does
        smallboards.fetch_list_page(ALUMNI.list_url)
    except Exception as e:  # noqa: BLE001
        swallowed.append(e)
    assert swallowed
    assert any("192.0.2.1" in a for a in no_network) and ALUMNI.list_url in no_network  # still recorded
    no_network.clear()  # these were on purpose
