"""Department and college boards on the shared PHP board engines, one gate per server."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from kwcampus import (
    aicon,
    collector,
    deptboards,
    dispatcher,
    fetch,
    politeness,
    public_data,
    routing,
    wixfeeds,
    wwwboards,
)
from kwcampus.config import KST
from kwcampus.fetch import FetchResult

FIXTURES = Path(__file__).parent / "fixtures"
# Saved from ee.kw.ac.kr/HTML/department/undergrad_info.php and law.kw.ac.kr/bulletin/notice.php on
# 2026-11-05 (public pages), cut down to a few real rows each.
EE_PAGE = (FIXTURES / "dept_ee_list.html").read_text()
LAW_PAGE = (FIXTURES / "dept_law_list.html").read_text()
# Saved from biz.kw.ac.kr/community/notice.php and sports.kw.ac.kr/community/notice.php on 2026-11-05,
# english.kw.ac.kr/new/bulletin/notice.php (EUC-KR) and chemng.kw.ac.kr/community/notice.php on
# 2026-11-06 (public pages), cut down to a few real rows each.
BIZ_PAGE = (FIXTURES / "dept_biz_list.html").read_text()
ENGLISH_RAW = (FIXTURES / "dept_english_list.html").read_bytes()
CHEMNG_PAGE = (FIXTURES / "dept_chemng_list.html").read_text()
SPORTS_PAGE = (FIXTURES / "dept_sports_list.html").read_text()
NOW = datetime(2026, 11, 5, 3, 0, tzinfo=UTC)  # 12:00 KST
BOARD = {b.key: b for b in deptboards.BOARDS}
EE, LAW, BIZ, ENGLISH, CHEMNG, SPORTS = (BOARD[k] for k in ("ee", "law", "biz", "english", "chemng", "sports"))
E1 = tuple(b for b in deptboards.BOARDS if b.server == deptboards.SERVER_E1)


def page(html):
    def fake(url, encoding="utf-8", errors="strict"):
        return FetchResult(html, None, 200)
    return fake


def notices(table, source):
    return {i["post_id"]: i for i in table.scan()["Items"] if i["PK"] == f"SRC#{source}" and i["SK"].startswith("N#")}


def test_parse_reads_pinned_and_normal_rows():
    rows = deptboards.parse_board(EE_PAGE, EE)
    assert [(r.duid, r.pinned) for r in rows] == [
        ("646", True), ("631", True), ("640", True), ("15", True), ("627", False), ("632", False), ("628", False)]
    # 전자공학과 posts new items as pinned: the newest pinned post is far newer than the newest normal one
    assert max(r.posted_date for r in rows if r.pinned).isoformat() == "2026-11-04"
    assert max(r.posted_date for r in rows if not r.pinned).isoformat() == "2026-03-05"
    first = rows[0]
    assert first.title == "[대학혁신지원사업] 2026 광운대학교 취업박람회 개최 안내"
    assert first.category_id == "dept-ee" and first.department == "전자공학과"
    assert first.source_url == "https://ee.kw.ac.kr/HTML/department/undergrad_info.php?BoardMode=view&UID=646"
    assert EE.list_url == "https://ee.kw.ac.kr/HTML/department/undergrad_info.php?BoardMode=list&BOARD_CODE=bachelor&CURRENT_PAGE=1"


def test_columns_are_found_by_header_text():
    # 법학부: 번호/제목/등록일/작성자, and its pinned rows come without <tr>
    rows = deptboards.parse_board(LAW_PAGE, LAW)
    assert [(r.duid, r.pinned, r.posted_date.isoformat()) for r in rows] == [
        ("518", True, "2026-11-02"), ("507", True, "2026-10-08"), ("519", False, "2026-11-04"), ("515", False, "2026-10-28")]
    assert all("관리" not in r.title for r in rows)  # the writer column is not mistaken for anything


def test_titles_are_redacted():
    html = EE_PAGE.replace("수강정정기간 관련 공지", "수강정정 문의 010-1234-5678 hong@kw.ac.kr")
    title = next(r.title for r in deptboards.parse_board(html, EE) if r.duid == "632")
    assert "010-1234-5678" not in title and "hong@" not in title and "[REDACTED_PHONE]" in title


def test_euc_kr_board_decodes(monkeypatch):
    # english.kw.ac.kr serves EUC-KR and cuts long titles at a byte count: one title here ends in half a character.
    cut = "강사 모집 홍보".encode("cp949")
    raw = ENGLISH_RAW.replace(cut, cut + b"\xb0")

    class Response:
        status, headers = 200, {"Content-Type": "text/html; charset=euc-kr"}

        def read(self, limit):
            return raw

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(fetch, "_opener", type("Opener", (), {"open": lambda self, request, timeout: Response()})())
    assert fetch.fetch_list_page(ENGLISH.list_url).block_reason == "markup"  # not UTF-8
    result = fetch.fetch_list_page(ENGLISH.list_url, encoding=ENGLISH.encoding, errors="replace")
    rows = deptboards.parse_board(result.html, ENGLISH)
    assert [(r.duid, r.pinned) for r in rows] == [("1302", False), ("1301", False), ("1300", False), ("1299", False)]
    assert rows[1].title == "[홍보] [B&Y영어학원] 강사 모집 홍보" and rows[0].department == "영어산업학과"
    assert rows[0].source_url == "https://english.kw.ac.kr/new/bulletin/notice.php?BoardMode=view&UID=1302"
    assert ENGLISH.list_url == "https://english.kw.ac.kr/new/bulletin/notice.php?BoardMode=list&BOARD_CODE=notice&CURRENT_PAGE=1"


def test_a_cdnjs_stylesheet_is_not_a_challenge(monkeypatch):
    # ingenium.kw.ac.kr's <head> loads Font Awesome from cdnjs.cloudflare.com; a real challenge still counts.
    link = '<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.1.2/css/all.min.css" />'
    pages = {"ingenium": EE_PAGE.replace("<head>", "<head>" + link),
             "challenge": EE_PAGE.replace("<head>", "<head><title>Just a moment...</title>" + link + "<p>cf-chl-opt</p>")}

    def opener(body):
        class Response:
            status, headers = 200, {"Content-Type": "text/html; charset=utf-8"}

            def read(self, limit):
                return body.encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return type("Opener", (), {"open": lambda self, request, timeout: Response()})()

    monkeypatch.setattr(fetch, "_opener", opener(pages["ingenium"]))
    result = fetch.fetch_list_page(BOARD["ingenium"].list_url, errors="replace")
    assert result.block_reason is None and deptboards.parse_board(result.html, BOARD["ingenium"])
    monkeypatch.setattr(fetch, "_opener", opener(pages["challenge"]))
    assert fetch.fetch_list_page(BOARD["ingenium"].list_url).block_reason == "captcha"


def test_numbers_with_commas_are_normal_rows():
    # 경영대학: 65 pinned rows, then normal rows numbered "2,175"
    rows = deptboards.parse_board(BIZ_PAGE, BIZ)
    assert [(r.duid, r.pinned) for r in rows] == [
        ("2551", True), ("2550", True), ("2539", True), ("2545", False), ("2509", False)]
    assert rows[4].title == "[취업] [비알코리아x파리크라상x삼립] 2026년 하반기 대졸 신입 공채 (~10/14까지)"
    assert BIZ.list_url == "https://biz.kw.ac.kr/community/notice.php?BoardMode=list&BOARD_CODE=note&CURRENT_PAGE=1"


def test_b_group_boards_read_admin_mode_links():
    sports = deptboards.parse_board(SPORTS_PAGE, SPORTS)
    assert [(r.duid, r.pinned, r.posted_date.isoformat()) for r in sports] == [
        ("68", True, "2026-05-27"), ("67", True, "2025-11-24"), ("69", False, "2026-08-24"), ("66", False, "2025-08-22")]
    assert sports[0].title == "2026학년도 2학기 성적우수 및 프론티어장학금 신청 안내"
    assert sports[0].source_url == "https://sports.kw.ac.kr/community/notice.php?admin_mode=read&no=68"
    chemng = deptboards.parse_board(CHEMNG_PAGE, CHEMNG)  # table.board-list, 작성일
    assert [(r.duid, r.pinned) for r in chemng] == [("78", False), ("77", False), ("76", False)]
    assert chemng[0].department == "화학공학과" and chemng[0].category_id == "dept-chemng"
    assert CHEMNG.list_url == "https://chemng.kw.ac.kr/community/notice.php"
    assert deptboards.parse_board(EE_PAGE, SPORTS) == [] and deptboards.parse_board(SPORTS_PAGE, EE) == []


def test_every_new_board_is_a_department_category():
    for b in deptboards.BOARDS:
        assert routing.CATEGORY_LABEL[b.category_id] == b.name and routing.channel_for(b.category_id) == "department"
    assert len({b.key for b in deptboards.BOARDS}) == len(deptboards.BOARDS)
    assert {b.key for b in deptboards.BOARDS}.isdisjoint({f.key for f in wixfeeds.FEEDS} | {"aicon"})


def test_seeds_quietly_then_treats_higher_uids_as_new(aws, monkeypatch):
    monkeypatch.setattr(deptboards, "BOARDS", (EE,))
    table = aws["public"]
    first = deptboards.collect(NOW, table, fetch=page(EE_PAGE))
    assert first == {deptboards.SERVER_E1: {"status": "ok", "board": "ee", "rows": 7, "seed_run": True,
                                            "inserted": 7, "updated": 0, "unchanged": 0}}
    assert all(i["seed"] for i in notices(table, "dept-ee").values())

    # A new post arrives pinned at the top; an old post never seen before slides back onto page 1.
    later = (EE_PAGE.replace("UID=646&", "UID=647&").replace("2026 광운대학교 취업박람회", "새 고정 공지")
             .replace("UID=628&", "UID=600&"))
    second = deptboards.collect(NOW + timedelta(minutes=20), table, fetch=page(later))
    assert second[deptboards.SERVER_E1]["inserted"] == 2 and second[deptboards.SERVER_E1]["seed_run"] is False
    rows = notices(table, "dept-ee")
    assert rows["647"]["seed"] is False and rows["647"]["pinned"] is True
    assert rows["600"]["seed"] is True  # below the highest UID seen: stored quietly, no alert

    def stream(item):
        return {"eventName": "INSERT", "dynamodb": {"NewImage": {
            "PK": {"S": item["PK"]}, "SK": {"S": item["SK"]}, "category_id": {"S": item["category_id"]},
            "seed": {"BOOL": item["seed"]}, "pinned": {"BOOL": item["pinned"]}, "posted_date": {"S": item["posted_date"]}}}}

    jobs = dispatcher.jobs_for([stream(rows["647"]), stream(rows["600"])], NOW.date())
    assert [(j["key"]["SK"], j["channel"]) for j in jobs] == [("N#647", "department")]


def test_b_group_board_seeds_then_finds_new_posts_by_number(aws, monkeypatch):
    monkeypatch.setattr(deptboards, "BOARDS", (SPORTS,))
    table = aws["public"]
    first = deptboards.collect(NOW, table, fetch=page(SPORTS_PAGE))[deptboards.SERVER_E4]
    assert first["board"] == "sports" and first["seed_run"] is True and first["inserted"] == 4
    later = SPORTS_PAGE.replace("no=69&", "no=70&").replace("3분기 운동건강센터", "4분기 운동건강센터")
    second = deptboards.collect(NOW + timedelta(minutes=20), table, fetch=page(later))[deptboards.SERVER_E4]
    assert second["inserted"] == 1
    new = notices(table, "dept-sports")["70"]
    assert new["seed"] is False and new["url"] == "https://sports.kw.ac.kr/community/notice.php?admin_mode=read&no=70"


def test_one_request_to_the_server_per_run_round_robin(aws, monkeypatch):
    monkeypatch.setattr(deptboards, "BOARDS", E1)
    table, calls = aws["public"], []

    def fake(url, encoding="utf-8", errors="strict"):
        calls.append(url)
        return FetchResult(EE_PAGE, None, 200)

    deptboards.collect(NOW, table, fetch=fake)
    again = deptboards.collect(NOW + timedelta(seconds=5), table, fetch=fake)  # a second request in the same run
    assert again == {deptboards.SERVER_E1: {"status": "skipped", "reason": "too_soon"}}
    assert calls == [deptboards.BOARDS[0].list_url]
    for run in range(1, len(deptboards.BOARDS) + 1):
        deptboards.collect(NOW + timedelta(minutes=20 * run), table, fetch=fake)
    assert calls == [b.list_url for b in deptboards.BOARDS] + [deptboards.BOARDS[0].list_url]
    origins = {i["PK"] for i in table.scan()["Items"] if i["PK"].startswith("ORIGIN#")}
    assert origins == {f"ORIGIN#{deptboards.SERVER_E1}"}  # one gate for all the host names


def test_each_server_gets_one_request_a_run_and_its_own_round(aws):
    table, calls = aws["public"], []

    def fake(url, encoding="utf-8", errors="strict"):
        calls.append(url)
        return FetchResult(EE_PAGE if "BoardMode=list" in url else SPORTS_PAGE, None, 200)

    rounds = {s: [b for b in deptboards.BOARDS if b.server == s] for s in dict.fromkeys(b.server for b in deptboards.BOARDS)}
    assert {s: len(r) for s, r in rounds.items()} == {
        deptboards.SERVER_E1: 8, deptboards.SERVER_E2: 8, deptboards.SERVER_E3: 5, deptboards.SERVER_E4: 2}
    for run in range(8):
        out = deptboards.collect(NOW + timedelta(minutes=20 * run), table, fetch=fake)
        assert {s: r["status"] for s, r in out.items()} == dict.fromkeys(rounds, "ok")
        assert len(calls) == 4 * (run + 1)  # one request to each server, never two to one
    for boards in rounds.values():  # least recently checked first: every board once per round
        urls = {b.list_url for b in boards}
        assert [u for u in calls if u in urls] == [boards[i % len(boards)].list_url for i in range(8)]
    origins = {i["PK"] for i in table.scan()["Items"] if i["PK"].startswith("ORIGIN#")}
    assert origins == {f"ORIGIN#{s}" for s in rounds}


def test_daily_cap_per_server(aws, monkeypatch):
    monkeypatch.setattr(deptboards, "BOARDS", E1)
    table = aws["public"]
    today = NOW.astimezone(KST).strftime("%Y-%m-%d")
    table.put_item(Item={"PK": f"ORIGIN#{deptboards.SERVER_E1}", "SK": "STATE", "day": today,
                         "day_count": deptboards.DAILY_CAP, "ver": 1})
    calls = []
    out = deptboards.collect(NOW, table, fetch=lambda url, **kw: calls.append(url))
    assert out == {deptboards.SERVER_E1: {"status": "skipped", "reason": "daily_cap"}} and calls == []
    tomorrow = politeness.acquire(table, deptboards.SERVER_E1, NOW + timedelta(days=1), daily_cap=deptboards.DAILY_CAP)
    assert tomorrow.granted


def test_a_block_cools_down_every_board_on_the_server(aws, monkeypatch):
    monkeypatch.setattr(deptboards, "BOARDS", E1)
    table, calls = aws["public"], []
    blocked = deptboards.collect(NOW, table, fetch=lambda url, **kw: FetchResult(None, "rate_limit", 429))
    assert blocked == {deptboards.SERVER_E1: {"status": "blocked", "board": "ei", "reason": "rate_limit"}}
    later = deptboards.collect(NOW + timedelta(minutes=20), table, fetch=lambda url, **kw: calls.append(url))
    assert later[deptboards.SERVER_E1]["reason"] == "cooldown" and calls == []
    after = deptboards.collect(NOW + timedelta(minutes=40), table, fetch=page(EE_PAGE))
    assert after[deptboards.SERVER_E1]["board"] == "ee"  # the blocked board waits for its next turn


def test_department_posts_are_searchable_with_distinct_ids(aws):
    table = aws["public"]
    deptboards.collect(NOW, table, fetch=page(EE_PAGE))  # ei (and archi, kwlife on E2, E3)
    deptboards.collect(NOW + timedelta(minutes=20), table, fetch=page(EE_PAGE))  # ee, same UIDs
    public_data.clear_cache()
    ei = public_data.latest(table, "전자정보공과대학", 50)
    ee = public_data.search(table, "전자공학과 수강", None, None, 50)
    assert ei and {r["department"] for r in ei} == {"전자정보공과대학"}
    assert ee and {r["category"] for r in ee} == {"전자공학과"} and all(r["post_id"].startswith("dept-ee-") for r in ee)
    assert public_data.get(table, "dept-ei-646")["department"] == "전자정보공과대학"
    assert public_data.get(table, "dept-ee-646")["department"] == "전자공학과"
    assert public_data.category_id("전정대") == "dept-ei" and public_data.category_id("인사대") == "dept-chss"
    assert {r["category"] for r in public_data.latest(table, "참빛", 50)} == {"참빛인재대학"}
    assert public_data.get(table, "dept-archi-646")["department"] == "건축공학과"
    counts = {c["category_id"]: c["count"] for c in public_data.categories(table)}
    assert counts["dept-ei"] == counts["dept-ee"] == counts["dept-archi"] == 7 and counts["dept-semicon"] == 0


def test_handler_runs_the_main_board_first_and_isolates_each_collector(aws, monkeypatch):
    aws["ssm"].put_parameter(Name="/kw/collector/enabled", Value="true", Type="String")
    order = []

    def main(now, table):
        order.append("main")
        return {"status": "ok"}

    def failing(name):
        def collect(now, table):
            order.append(name)
            raise RuntimeError(f"{name} down")
        return collect

    def not_due(name):
        def collect(now, table):
            order.append(name)
            return {"status": "not_due"}
        return collect

    monkeypatch.setattr(collector, "run", main)
    monkeypatch.setattr(aicon, "collect", failing("aicon"))
    monkeypatch.setattr(deptboards, "collect", failing("dept"))
    monkeypatch.setattr(wixfeeds, "collect", not_due("wix"))
    monkeypatch.setattr(wwwboards, "collect", not_due("www"))
    monkeypatch.setattr(collector.boto3, "resource", lambda name: type("R", (), {"Table": lambda self, n: aws["public"]})())
    assert collector.handler({}, None) == {"status": "ok", "aicon": {"status": "error"}, "dept": {"status": "error"},
                                           "wix": {"status": "not_due"}, "www": {"status": "not_due"}}
    assert order == ["main", "aicon", "dept", "wix", "www"]


def test_a_changed_page_does_not_hold_up_the_other_boards(aws):
    table = aws["public"]
    out = deptboards.collect(NOW, table, fetch=page("<html><body>새 디자인</body></html>"))
    assert next(iter(out.values()))["status"] == "no_rows"
    later = NOW + timedelta(minutes=20)
    assert politeness.acquire(table, EE.server, later, daily_cap=deptboards.DAILY_CAP).granted  # no cooldown


def test_a_whole_round_of_empty_pages_backs_the_server_off(aws):
    table = aws["public"]
    empty = page("<html><body>점검 중</body></html>")
    at = NOW
    servers = [b for b in deptboards.BOARDS if b.server == EE.server]
    for _ in servers:
        deptboards.collect(at, table, fetch=empty)
        at += timedelta(minutes=20)
    assert not politeness.acquire(table, EE.server, at, daily_cap=deptboards.DAILY_CAP).granted  # cooling down
