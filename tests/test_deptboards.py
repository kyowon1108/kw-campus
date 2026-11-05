"""Department boards on the shared PHP board software (전자정보공과대학 and its departments)."""

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
    wwwboards,
)
from kwcampus.config import KST
from kwcampus.fetch import FetchResult

FIXTURES = Path(__file__).parent / "fixtures"
# Saved from ee.kw.ac.kr/HTML/department/undergrad_info.php and law.kw.ac.kr/bulletin/notice.php on
# 2026-11-05 (public pages), cut down to a few real rows each.
EE_PAGE = (FIXTURES / "dept_ee_list.html").read_text()
LAW_PAGE = (FIXTURES / "dept_law_list.html").read_text()
NOW = datetime(2026, 11, 5, 3, 0, tzinfo=UTC)  # 12:00 KST
EE = next(b for b in deptboards.BOARDS if b.key == "ee")
LAW = deptboards.Board("law", "법학부", "law.kw.ac.kr", "/bulletin/notice.php", "notice")


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
    # english.kw.ac.kr serves EUC-KR and cuts long titles at a byte count; its list page was not saved,
    # so the 전자공학과 rows are re-encoded here, with one title cut in the middle of a character.
    raw = EE_PAGE.replace("수강정정기간 관련 공지", "수강정정기간 관련 공지@@").encode("cp949").replace(b"@@", b"\xb0")

    class Response:
        status, headers = 200, {"Content-Type": "text/html; charset=euc-kr"}

        def read(self, limit):
            return raw

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(fetch, "_opener", type("Opener", (), {"open": lambda self, request, timeout: Response()})())
    assert fetch.fetch_list_page("https://english.kw.ac.kr/x.php").block_reason == "markup"  # not UTF-8
    result = fetch.fetch_list_page("https://english.kw.ac.kr/x.php", encoding="cp949", errors="replace")
    rows = deptboards.parse_board(result.html, EE)
    assert [r.title for r in rows] == [r.title for r in deptboards.parse_board(EE_PAGE, EE)]


def test_seeds_quietly_then_treats_higher_uids_as_new(aws, monkeypatch):
    monkeypatch.setattr(deptboards, "BOARDS", (EE,))
    table = aws["public"]
    first = deptboards.collect(NOW, table, fetch=page(EE_PAGE))
    assert first == {deptboards.SERVER_A: {"status": "ok", "board": "ee", "rows": 7, "seed_run": True,
                                           "inserted": 7, "updated": 0, "unchanged": 0}}
    assert all(i["seed"] for i in notices(table, "dept-ee").values())

    # A new post arrives pinned at the top; an old post never seen before slides back onto page 1.
    later = (EE_PAGE.replace("UID=646&", "UID=647&").replace("2026 광운대학교 취업박람회", "새 고정 공지")
             .replace("UID=628&", "UID=600&"))
    second = deptboards.collect(NOW + timedelta(minutes=20), table, fetch=page(later))
    assert second[deptboards.SERVER_A]["inserted"] == 2 and second[deptboards.SERVER_A]["seed_run"] is False
    rows = notices(table, "dept-ee")
    assert rows["647"]["seed"] is False and rows["647"]["pinned"] is True
    assert rows["600"]["seed"] is True  # below the highest UID seen: stored quietly, no alert

    def stream(item):
        return {"eventName": "INSERT", "dynamodb": {"NewImage": {
            "PK": {"S": item["PK"]}, "SK": {"S": item["SK"]}, "category_id": {"S": item["category_id"]},
            "seed": {"BOOL": item["seed"]}, "pinned": {"BOOL": item["pinned"]}, "posted_date": {"S": item["posted_date"]}}}}

    jobs = dispatcher.jobs_for([stream(rows["647"]), stream(rows["600"])], NOW.date())
    assert [(j["key"]["SK"], j["channel"]) for j in jobs] == [("N#647", "department")]


def test_one_request_to_the_server_per_run_round_robin(aws):
    table, calls = aws["public"], []

    def fake(url, encoding="utf-8", errors="strict"):
        calls.append(url)
        return FetchResult(EE_PAGE, None, 200)

    deptboards.collect(NOW, table, fetch=fake)
    again = deptboards.collect(NOW + timedelta(seconds=5), table, fetch=fake)  # a second request in the same run
    assert again == {deptboards.SERVER_A: {"status": "skipped", "reason": "too_soon"}}
    assert calls == [deptboards.BOARDS[0].list_url]
    for run in range(1, len(deptboards.BOARDS) + 1):
        deptboards.collect(NOW + timedelta(minutes=20 * run), table, fetch=fake)
    assert calls == [b.list_url for b in deptboards.BOARDS] + [deptboards.BOARDS[0].list_url]
    origins = {i["PK"] for i in table.scan()["Items"] if i["PK"].startswith("ORIGIN#")}
    assert origins == {f"ORIGIN#{deptboards.SERVER_A}"}  # one gate for all the host names


def test_daily_cap_per_server(aws):
    table = aws["public"]
    today = NOW.astimezone(KST).strftime("%Y-%m-%d")
    table.put_item(Item={"PK": f"ORIGIN#{deptboards.SERVER_A}", "SK": "STATE", "day": today,
                         "day_count": deptboards.DAILY_CAP, "ver": 1})
    calls = []
    out = deptboards.collect(NOW, table, fetch=lambda url, **kw: calls.append(url))
    assert out == {deptboards.SERVER_A: {"status": "skipped", "reason": "daily_cap"}} and calls == []
    tomorrow = politeness.acquire(table, deptboards.SERVER_A, NOW + timedelta(days=1), daily_cap=deptboards.DAILY_CAP)
    assert tomorrow.granted


def test_a_block_cools_down_every_board_on_the_server(aws):
    table, calls = aws["public"], []
    blocked = deptboards.collect(NOW, table, fetch=lambda url, **kw: FetchResult(None, "rate_limit", 429))
    assert blocked == {deptboards.SERVER_A: {"status": "blocked", "board": "ei", "reason": "rate_limit"}}
    later = deptboards.collect(NOW + timedelta(minutes=20), table, fetch=lambda url, **kw: calls.append(url))
    assert later[deptboards.SERVER_A]["reason"] == "cooldown" and calls == []
    after = deptboards.collect(NOW + timedelta(minutes=40), table, fetch=page(EE_PAGE))
    assert after[deptboards.SERVER_A]["board"] == "ee"  # the blocked board waits for its next turn


def test_department_posts_are_searchable_with_distinct_ids(aws):
    table = aws["public"]
    deptboards.collect(NOW, table, fetch=page(EE_PAGE))  # ei
    deptboards.collect(NOW + timedelta(minutes=20), table, fetch=page(EE_PAGE))  # ee, same UIDs
    public_data.clear_cache()
    ei = public_data.latest(table, "전자정보공과대학", 50)
    ee = public_data.search(table, "전자공학과 수강", None, None, 50)
    assert ei and {r["department"] for r in ei} == {"전자정보공과대학"}
    assert ee and {r["category"] for r in ee} == {"전자공학과"} and all(r["post_id"].startswith("dept-ee-") for r in ee)
    assert public_data.get(table, "dept-ei-646")["department"] == "전자정보공과대학"
    assert public_data.get(table, "dept-ee-646")["department"] == "전자공학과"
    assert public_data.category_id("전정대") == "dept-ei"
    counts = {c["category_id"]: c["count"] for c in public_data.categories(table)}
    assert counts["dept-ei"] == counts["dept-ee"] == 7 and counts["dept-semicon"] == 0


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

    def www(now, table):
        order.append("www")
        return {"status": "not_due"}

    monkeypatch.setattr(collector, "run", main)
    monkeypatch.setattr(aicon, "collect", failing("aicon"))
    monkeypatch.setattr(deptboards, "collect", failing("dept"))
    monkeypatch.setattr(wwwboards, "collect", www)
    monkeypatch.setattr(collector.boto3, "resource", lambda name: type("R", (), {"Table": lambda self, n: aws["public"]})())
    assert collector.handler({}, None) == {"status": "ok", "aicon": {"status": "error"}, "dept": {"status": "error"},
                                           "www": {"status": "not_due"}}
    assert order == ["main", "aicon", "dept", "www"]


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
