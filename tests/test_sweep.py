"""The nightly sweep (sweep.py): older notices with no finished read, read again a little at a time from the day's
leftover requests. Synthetic notices; the network is a fake fetch and reader. Some tests come from a reviewer's
reproductions (weekday spare, no_body counting, blocked pages, 총학생회 records, isolation, cross-server assets,
missing keys, poorer reads, reserve values, the time floor)."""

from datetime import datetime, timedelta

import anthropic
import pytest
import test_extract
from botocore.exceptions import ClientError
from test_extract import (
    EE_PAGE,
    EE_URL,
    PNG,
    WWW_PAGE,
    WWW_URL,
    FakeClaude,
    api_error,
    fetcher,
    item,
    keys,
    notice,
)
from test_reading import EMPTY_PAGE, POSTER_A, Clock, reader

from kwcampus import (
    collector,
    extract,
    notice_assets,
    opsdigest,
    params,
    politeness,
    store,
    sweep,
)
from kwcampus.config import KST
from kwcampus.fetch import FetchResult
from kwcampus.kwnotice.source import NOTICE_HOST

isolated = test_extract.isolated
THURSDAY_NIGHT = datetime(2026, 11, 5, 22, 0, 5, tzinfo=KST)  # after the day's last list run (21:00)
SATURDAY_NIGHT = datetime(2026, 11, 7, 22, 0, 5, tzinfo=KST)  # no list runs on a weekend
NIGHT = SATURDAY_NIGHT
EE2_URL = EE_URL.replace("646", "647")
ARCHI_URL = "https://archi.kw.ac.kr/community/notice.php?BoardMode=view&UID=11"
ARCHI_PAGE = '<div class="view_con"><p>건축공학과 졸업설계 전시회를 11월 25일부터 엽니다. 많은 관심 바랍니다.</p></div>'
ALUMNI_URL = "https://www.kwuaa.com/bbs/board.php?bo_table=notice&wr_id=90"
ALUMNI_PAGE = ('<article id="bo_v"><section id="bo_v_info"><strong class="if_date">26-11-01 10:00</strong></section>'
               '<div id="bo_v_con"><p>총동문회 장학생 선발: 신청은 11월 20일까지 사무국으로 보내 주세요.</p></div></article>')
PAGES = {WWW_URL: WWW_PAGE, EE_URL: EE_PAGE, EE2_URL: EE_PAGE.replace("20일", "21일"), ARCHI_URL: ARCHI_PAGE,
         ALUMNI_URL: ALUMNI_PAGE}
SOURCES = ["kw-notice", "dept-ee", "dept-archi", "internship", "dept-media", "council"]


@pytest.fixture(autouse=True)
def few_sources(monkeypatch):
    monkeypatch.setattr(sweep, "sources", lambda: list(SOURCES))


def put(table, source, duid, url, posted="2026-11-01", seed=True, **extra):
    """A stored notice as the collectors left it: no queue row (a seed), and no read record unless given."""
    category = {"kw-notice": "academic", "internship": "office-internship", "council": "office-council",
                "alumni": "office-alumni"}.get(source, source)
    n = notice(source, duid, category, url, posted=datetime.fromisoformat(posted).date(), seed=seed)
    table.put_item(Item={**n, **extra})
    return {"PK": n["PK"], "SK": n["SK"]}


def go(table, at=NIGHT, pages=PAGES, claude=None, calls=None, log=None, pictures=(), fetch=None, **kw):
    clock = Clock(at)
    return sweep.collect(at, table, fetch=fetch or fetcher(calls if calls is not None else [], pages), clock=clock,
                         sleep=clock.sleep, anthropic_client=claude or FakeClaude(),
                         read=reader(list(pictures), log if log is not None else []), **kw)


def used(table, origin: str, at: datetime, count: int) -> None:
    table.put_item(Item={"PK": f"ORIGIN#{origin}", "SK": "STATE", "day": at.astimezone(KST).strftime("%Y-%m-%d"),
                         "day_count": count})


def state(table):
    return table.get_item(Key=sweep.STATE)["Item"]


# --- the night's leftover ---------------------------------------------------------------------------------------

def test_a_weekday_night_leaves_the_department_servers_nothing_and_a_weekend_night_plenty(aws, monkeypatch):
    """After a full weekday the department servers have used 31 of their 32 requests on list runs: one left, under
    the margin. 총동문회's server, checked every 4 hours, has most of its cap left. On a weekend everything is."""
    monkeypatch.setattr(sweep, "sources", lambda: ["dept-ee", "alumni"])
    table = aws["public"]
    keys(aws)
    put(table, "dept-ee", 646, EE_URL)
    put(table, "alumni", 90, ALUMNI_URL)
    ee = extract.page_for_host("ee.kw.ac.kr")
    alumni = extract.page_for_host("www.kwuaa.com")
    assert extract.runs_left(THURSDAY_NIGHT) == 0 and extract._reserve(ee, THURSDAY_NIGHT) == 0
    used(table, ee.origin, THURSDAY_NIGHT, len(extract._LIST_RUNS))  # 31 list runs
    used(table, alumni.origin, THURSDAY_NIGHT, len(extract.check_slots(alumni.check_every)))  # 4 checks
    assert extract.spare(table, ee, THURSDAY_NIGHT) == 1 < notice_assets.margin(ee) + 1
    calls = []
    out = go(table, THURSDAY_NIGHT, calls=calls)
    assert calls == [ALUMNI_URL] and out["sweep_no_spare"] == 1  # E1: nothing above its margin
    calls.clear()
    out = go(table, SATURDAY_NIGHT, calls=calls, pictures=[(POSTER_A, PNG)])  # a weekend: the day's whole cap
    assert calls == [EE_URL] and out["sweep_extracted"] == 1
    assert item(table, {"PK": "SRC#dept-ee", "SK": "N#646"})["read"]["status"] == "complete"  # its poster too


def test_margins_scale_with_the_servers_cap():
    assert notice_assets.margin(extract.page_for_host("ee.kw.ac.kr")) == 3  # 32
    assert notice_assets.margin(extract.page_for_host(NOTICE_HOST)) == 4  # 44
    assert notice_assets.margin(extract.page_for_host("www.kwuaa.com")) == 1  # 12


def test_an_image_on_another_server_keeps_that_servers_margin(aws, monkeypatch):
    monkeypatch.setattr(sweep, "sources", lambda: ["dept-ee"])
    table = aws["public"]
    keys(aws)
    key = put(table, "dept-ee", 646, EE_URL)  # no facts yet
    www = extract.page_for_host(NOTICE_HOST)  # the test reader's poster is on www
    claude, outs = FakeClaude(), []
    for night in range(2):
        at = NIGHT + timedelta(days=night)
        used(table, www.origin, at, www.daily_cap - notice_assets.margin(www) - extract._reserve(www, at))
        outs.append(go(table, at, calls=(calls := []), claude=claude, pictures=[(POSTER_A, PNG)]))
        assert calls == [EE_URL]
        assert extract.spare(table, www, at) == notice_assets.margin(www)  # www's margin untouched
        if night == 0:  # its poster unread: no call on the text alone, another night
            assert outs[0]["sweep_waiting"] == 1 and claude.calls == [] and "read" not in item(table, key)
            assert int(item(table, key)["sweep_after"]) == int(datetime(2026, 11, 8, tzinfo=KST).timestamp())
    assert outs[1]["sweep_extracted"] == 1 and len(claude.calls) == 1  # its last sweep: the text, long enough
    stored = item(table, key)
    assert stored["read"]["status"] == "partial" and stored["read"]["images"]["skipped"] == {"budget": 1}
    assert stored["extracted"]["sources_used"] == ["body"] and stored["sweeps"] == 2 and "sweep_after" not in stored


def test_too_little_text_and_an_unread_poster_end_partial_without_a_call(aws, monkeypatch):
    monkeypatch.setattr(sweep, "sources", lambda: ["kw-notice"])
    table = aws["public"]
    keys(aws)
    key = put(table, "kw-notice", 1001, WWW_URL)
    www = extract.page_for_host(NOTICE_HOST)
    claude, calls, outs = FakeClaude(), [], []
    for night in range(3):
        at = NIGHT + timedelta(days=night)
        used(table, www.origin, at, www.daily_cap - extract._reserve(www, at) - notice_assets.margin(www) - 2)
        outs.append(go(table, at, pages={WWW_URL: EMPTY_PAGE}, calls=calls, claude=claude, pictures=[(POSTER_A, PNG)]))
    assert calls == [WWW_URL, WWW_URL] and claude.calls == []
    assert outs[0]["sweep_waiting"] == 1 and outs[1]["sweep_recorded"] == 1 and outs[2] == {"status": "idle"}
    stored = item(table, key)
    assert (stored["read"]["status"], stored["read"]["body"]) == ("partial", "empty") and "extract_v" not in stored


# --- what is read, and how much -------------------------------------------------------------------------------

def test_one_notice_a_server_a_run_newest_first_and_the_cursor_moves_on(aws):
    table = aws["public"]
    keys(aws)
    put(table, "dept-ee", 646, EE_URL, posted="2026-10-01")
    put(table, "dept-ee", 647, EE2_URL, posted="2026-11-02")  # same server (E1), newer
    put(table, "kw-notice", 1001, WWW_URL)
    put(table, "dept-archi", 11, ARCHI_URL)  # E2
    calls, claude = [], FakeClaude()
    out = go(table, calls=calls, claude=claude)
    assert sorted(calls) == sorted([EE2_URL, WWW_URL, ARCHI_URL]) and out["sweep_extracted"] == 3
    stored = item(table, {"PK": "SRC#dept-ee", "SK": "N#647"})
    assert stored["read"]["status"] == "body_only" and stored["sweeps"] == 1 and "sweep_after" not in stored
    assert "read" not in item(table, {"PK": "SRC#dept-ee", "SK": "N#646"})
    calls.clear()
    go(table, NIGHT + timedelta(minutes=20), calls=calls, claude=claude)
    assert calls == []  # the other three sources: nothing to read there
    go(table, NIGHT + timedelta(minutes=40), calls=calls, claude=claude)
    assert calls == [EE_URL]  # back round: E1's next one


def test_a_queued_notice_is_the_queues(aws):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("dept-ee", 646, "dept-ee", EE_URL))
    go(table, calls=(calls := []))
    assert calls == [] and state(table)["left"]["dept-ee"] == 0


def test_model_calls_come_only_from_the_cap_above_the_reserve(aws):
    table = aws["public"]
    keys(aws)
    put(table, "dept-ee", 646, EE_URL)  # no facts: it would need a call
    put(table, "kw-notice", 1001, WWW_URL, extract_v=1, extracted={"deadlines": [], "sources_used": ["body"]},
        body_hash="x", body_hash_by="page", extracted_at=int(NIGHT.timestamp()))
    today = NIGHT.astimezone(KST).date().isoformat()
    table.put_item(Item={"PK": "EXTRACT", "SK": "DAY", "day": today, "n": 40 - extract.DEFAULT_SWEEP_RESERVE})
    calls, claude = [], FakeClaude()
    out = go(table, calls=calls, claude=claude, cap=40)
    assert calls == [WWW_URL] and claude.calls == []  # E1's has no facts: not even fetched
    assert out["sweep_no_model"] == 1  # www's text changed: it needs a call, none left: it waits for tomorrow
    www = item(table, {"PK": "SRC#kw-notice", "SK": "N#1001"})
    assert "read" not in www and int(www["sweep_after"]) > NIGHT.timestamp() and www["sweeps"] == 1
    go(table, NIGHT + timedelta(minutes=60), calls=(again := []), claude=claude, cap=40)
    assert WWW_URL not in again  # not fetched again tonight


def test_a_reserve_above_the_cap_leaves_the_sweep_no_call_and_no_page_without_facts(aws):
    table = aws["public"]
    keys(aws)
    aws["ssm"].put_parameter(Name=extract.SWEEP_RESERVE_PARAM, Value="99", Type="String")
    params.clear_cache()
    put(table, "dept-ee", 646, EE_URL)
    put(table, "kw-notice", 1001, WWW_URL, extract_v=1, extracted={"deadlines": [], "sources_used": ["body"]},
        body_hash="x", body_hash_by="page", extracted_at=int(NIGHT.timestamp()))
    claude = FakeClaude()
    out = go(table, calls=(calls := []), claude=claude, cap=40)
    assert claude.calls == [] and calls == [WWW_URL] and out["sweep_no_model"] == 1
    assert extract.used_today(table, NIGHT.astimezone(KST).date().isoformat()) == 0


@pytest.mark.parametrize(("raw", "want"), [(None, 15), ("abc", 15), ("-3", 15), (" 7 ", 7), ("0", 0), ("1.5", 15),
                                           ("99", 99)])
def test_the_reserve_setting(aws, raw, want):
    if raw is not None:
        aws["ssm"].put_parameter(Name=extract.SWEEP_RESERVE_PARAM, Value=raw, Type="String")
    params.clear_cache()
    assert extract.sweep_reserve() == want


def test_no_page_without_a_model_provider(aws):
    table = aws["public"]
    keys(aws, claude=False, openai=False)
    put(table, "kw-notice", 1001, WWW_URL)
    for i in range(3):
        out = go(table, NIGHT + timedelta(minutes=20 * i), calls=(calls := []))
        assert calls == [] and out.get("sweep_no_provider", 0) <= 1


# --- facts are never made poorer ------------------------------------------------------------------------------

def test_a_read_that_brings_less_than_the_facts_came_from_never_replaces_them(aws):
    table = aws["public"]
    keys(aws)
    week = int(timedelta(days=8).total_seconds())
    digest = extract.body_hash(extract.body_text(WWW_PAGE, extract._PAGES["kw-notice"].selector))
    facts = {"deadlines": [{"kind": "행사", "at": "2026-11-25", "text": "포스터: 설명회 11/25"}],
             "sources_used": ["body", "images", "attachments"], "amount": "50만원"}
    earlier = {"status": "partial", "body": "ok", "tries": 1, "last_try_at": int(NIGHT.timestamp()) - week,
               "images": {"found": 1, "read": 1, "skipped": {}}, "files": {"found": 1, "read": 1, "skipped": {}}}
    key = put(table, "kw-notice", 1001, WWW_URL, extract_v=1, extracted=facts, body_hash=digest, body_hash_by="page",
              extracted_at=int(NIGHT.timestamp()) - week, read=earlier)
    claude = FakeClaude(answer={**test_extract.ANSWER, "amount": "", "deadlines": []})
    out = go(table, claude=claude, pictures=[(POSTER_A, PNG)])  # the poster, but no file this time
    assert out["sweep_recorded"] == 1 and claude.calls == []
    stored = item(table, key)
    assert stored["extracted"] == facts
    assert stored["read"]["images"] == earlier["images"] and stored["read"]["files"] == earlier["files"]


def test_a_changed_text_with_skipped_assets_waits_instead_of_a_call_on_the_text_alone(aws):
    table = aws["public"]
    keys(aws)
    facts = {"deadlines": [], "sources_used": ["body", "images"], "amount": "50만원"}
    key = put(table, "kw-notice", 1001, WWW_URL, extract_v=1, extracted=facts, body_hash="0" * 16,
              body_hash_by="page", extracted_at=int(NIGHT.timestamp()))
    www = extract.page_for_host(NOTICE_HOST)
    used(table, www.origin, NIGHT, www.daily_cap - extract._reserve(www, NIGHT) - notice_assets.margin(www) - 2)
    claude = FakeClaude()
    out = go(table, claude=claude, pictures=[(POSTER_A, PNG)])  # the page goes; its poster can't
    assert out["sweep_waiting"] == 1 and claude.calls == []
    stored = item(table, key)
    assert stored["extracted"] == facts and int(stored["sweep_after"]) > NIGHT.timestamp()


def test_a_new_source_gets_the_model_and_merges(aws):
    table = aws["public"]
    keys(aws)
    digest = extract.body_hash(extract.body_text(WWW_PAGE, extract._PAGES["kw-notice"].selector))
    key = put(table, "kw-notice", 1001, WWW_URL, extract_v=1, body_hash=digest, body_hash_by="page",
              extracted={"deadlines": [], "sources_used": ["body"], "amount": "50만원"},
              extracted_at=int(NIGHT.timestamp()))
    claude = FakeClaude(answer={**test_extract.ANSWER, "amount": ""})
    out = go(table, claude=claude, pictures=[(POSTER_A, PNG)])
    assert out["sweep_extracted"] == 1 and len(claude.calls) == 1
    stored = item(table, key)
    assert stored["extracted"]["amount"] == "50만원" and stored["extracted"]["sources_used"] == ["body", "images"]


def test_no_model_call_when_the_read_brings_nothing_new(aws):
    table = aws["public"]
    keys(aws)
    digest = extract.body_hash(extract.body_text(WWW_PAGE, extract._PAGES["kw-notice"].selector))
    key = put(table, "kw-notice", 1001, WWW_URL, extract_v=1, body_hash=digest, body_hash_by="page",
              extracted={"deadlines": [], "sources_used": ["body", "images"]}, extracted_at=int(NIGHT.timestamp()))
    claude = FakeClaude()
    assert go(table, claude=claude, pictures=[(POSTER_A, PNG)])["sweep_recorded"] == 1 and claude.calls == []
    assert item(table, key)["read"]["status"] == "complete"


# --- outcomes that can't finish tonight -----------------------------------------------------------------------

def test_a_changed_layout_counts_a_sweep_and_ends_after_two(aws, monkeypatch):
    monkeypatch.setattr(sweep, "sources", lambda: ["dept-ee"])
    table = aws["public"]
    keys(aws)
    key = put(table, "dept-ee", 646, EE_URL)
    changed = {EE_URL: "<html><body><div class='other-layout'>본문</div></body></html>"}
    fetched, at = [], NIGHT
    for _ in range(4):  # a night every eight days
        out = go(table, at, pages=changed, calls=(calls := []))
        fetched.append(len(calls))
        at += timedelta(days=8)
    assert fetched == [1, 1, 0, 0] and out == {"status": "idle"}  # twice, then never again
    stored = item(table, key)
    assert stored["sweeps"] == 2 and (stored["read"]["last_reason"], stored["read"]["tries"]) == ("no_body", 2)


@pytest.mark.parametrize("where", ["fetch", "facts"])
def test_a_crash_mid_read_still_counts_the_sweep(aws, monkeypatch, where):
    monkeypatch.setattr(sweep, "sources", lambda: ["dept-ee"])
    table = aws["public"]
    keys(aws)
    key = put(table, "dept-ee", 646, EE_URL)
    calls = []

    def fetch(url, encoding="utf-8", errors="strict"):
        calls.append(url)
        if where == "fetch":
            raise RuntimeError("boom")
        return FetchResult(EE_PAGE, None, 200)

    if where == "facts":  # after the page and the model call
        monkeypatch.setattr(extract, "validate", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad")))
    outs = [go(table, NIGHT + timedelta(days=night, minutes=20 * run), fetch=fetch)["status"]
            for night in range(3) for run in range(6)]
    assert calls == [EE_URL, EE_URL] and outs.count("error") == 2  # once a night, twice in all
    assert item(table, key)["sweeps"] == 2


def test_a_busy_model_counts_a_sweep_and_waits_for_the_next_night(aws, monkeypatch):
    monkeypatch.setattr(sweep, "sources", lambda: ["kw-notice"])
    table = aws["public"]
    keys(aws, openai=False)
    key = put(table, "kw-notice", 1001, WWW_URL)
    busy = FakeClaude(error=api_error(anthropic.RateLimitError, 429))
    calls = []
    assert go(table, calls=calls, claude=busy)["sweep_stop"] == 1 and calls == [WWW_URL]
    stored = item(table, key)
    assert stored["sweeps"] == 1 and int(stored["sweep_after"]) == int(datetime(2026, 11, 8, tzinfo=KST).timestamp())
    for minutes in (20, 40, 100):
        go(table, NIGHT + timedelta(minutes=minutes), calls=calls, claude=busy)
    assert calls == [WWW_URL]  # not again tonight
    out = go(table, NIGHT + timedelta(days=1), calls=calls)
    assert calls == [WWW_URL, WWW_URL] and out["sweep_extracted"] == 1


def test_a_page_that_keeps_failing_does_not_starve_the_servers_others(aws, monkeypatch):
    monkeypatch.setattr(sweep, "sources", lambda: ["dept-ee"])
    table = aws["public"]
    keys(aws)
    put(table, "dept-ee", 646, EE_URL, posted="2026-10-01")
    put(table, "dept-ee", 647, EE2_URL, posted="2026-11-02")  # the newest; its page always answers 500
    calls = []

    def fetch(url, encoding="utf-8", errors="strict"):
        calls.append(url)
        return FetchResult(None, "server_error", 500) if url == EE2_URL else FetchResult(EE_PAGE, None, 200)

    at = NIGHT
    for _ in range(3):
        go(table, at, fetch=fetch)
        politeness.record_success(table, "61.111.8.4")
        at += timedelta(minutes=20)
    assert calls == [EE2_URL, EE_URL]  # the 500 once, then the next one; nothing more tonight
    blocked = item(table, {"PK": "SRC#dept-ee", "SK": "N#647"})
    assert blocked["sweeps"] == 1 and int(blocked["sweep_after"]) > NIGHT.timestamp()


# --- no page to read: records without a request ---------------------------------------------------------------

def test_notices_with_no_page_get_their_record_without_a_request(aws, monkeypatch):
    table = aws["public"]
    keys(aws)
    monkeypatch.setattr(sweep, "SOURCES_PER_RUN", len(SOURCES))
    put(table, "internship", 7, "https://internship.kw.ac.kr/home/notice/view.do?bbsSeq=7")
    put(table, "dept-media", "6aeb0000aaaaaaaaaaaaaaaa", "https://www.kwmedia.info/post/x")
    put(table, "council", "cl1789744612718sbnrjp7", "https://www.kwu-studentcouncil.co.kr/notices/cl1789744612718sbnrjp7")
    put(table, "council", "cl1799000000000newpost1", "https://www.kwu-studentcouncil.co.kr/notices/cl1799000000000newpost1",
        seed=False)
    out = go(table, pages={}, calls=(calls := []))
    assert calls == [] and out["sweep_recorded"] == 4
    reasons = {(i["PK"], i["read"]["last_reason"]) for i in table.scan()["Items"] if i.get("read")}
    assert reasons == {("SRC#internship", "title_only_robots"), ("SRC#dept-media", "no_reader"),
                       ("SRC#council", "seed_no_text"), ("SRC#council", "no_text")}


def test_a_councils_post_read_before_keeps_its_record(aws, monkeypatch):
    monkeypatch.setattr(sweep, "sources", lambda: ["council"])
    table = aws["public"]
    keys(aws)
    week = int(timedelta(days=8).total_seconds())
    read = {"status": "partial", "body": "ok", "images": {"found": 1, "read": 0, "skipped": {"budget": 1}},
            "files": {"found": 0, "read": 0, "skipped": {}}, "tries": 3, "last_try_at": int(NIGHT.timestamp()) - week}
    facts = {"deadlines": [], "sources_used": ["body"], "summary": "총학 행사 신청"}
    key = put(table, "council", "cl1799000000000newpost1",
              "https://www.kwu-studentcouncil.co.kr/notices/cl1799000000000newpost1", seed=False,
              read=read, extracted=facts, extract_v=1)
    out = go(table, pages={})
    assert item(table, key)["read"] == read and "sweep_recorded" not in out
    assert state(table)["left"]["council"] == 0


def test_records_without_a_request_are_capped_and_need_time(aws, monkeypatch):
    table = aws["public"]
    keys(aws)
    monkeypatch.setattr(sweep, "RECORDS_PER_RUN", 2)
    monkeypatch.setattr(sweep, "sources", lambda: ["internship"])
    for i in range(5):
        put(table, "internship", i, f"https://internship.kw.ac.kr/home/notice/view.do?bbsSeq={i}")
    assert go(table, pages={})["sweep_recorded"] == 2
    assert state(table)["left"]["internship"] == 3
    out = go(table, NIGHT + timedelta(minutes=20), pages={}, time_left=lambda: extract.NEEDED_S + 1)
    assert out["sweep_recorded"] == 2  # time for records
    assert go(table, NIGHT + timedelta(minutes=40), pages={}, time_left=lambda: 5.0) == {
        "status": "skipped", "reason": "no_time"}  # below the floor: nothing at all
    draining = iter([extract.NEEDED_S] + [sweep.RECORDS_MIN_S - 1] * 100)  # enough to start, then not for records
    assert go(table, NIGHT + timedelta(minutes=60), pages={}, time_left=lambda: next(draining)) == {"status": "idle"}
    assert state(table)["left"]["internship"] == 1


# --- switches, isolation, the cursor --------------------------------------------------------------------------

@pytest.mark.parametrize(("raw", "on"), [(None, True), ("true", True), (" TRUE ", True), ("1", True), ("on", True),
                                         ("On", True), ("false", False), ("0", False), ("off", False), ("no", False),
                                         ("disabled", False), ("yes", False)])
def test_the_sweeps_own_switch(aws, raw, on):
    table = aws["public"]
    keys(aws)
    put(table, "kw-notice", 1001, WWW_URL)
    if raw is not None:
        aws["ssm"].put_parameter(Name=sweep.SWITCH_PARAM, Value=raw, Type="String")
    params.clear_cache()
    out = go(table, calls=(calls := []))
    if on:
        assert out["sweep_extracted"] == 1 and calls == [WWW_URL]
    else:
        assert out == {"status": "off"} and calls == []


def test_a_switch_that_cant_be_read_stops_the_run(aws, monkeypatch, capsys):
    table = aws["public"]
    keys(aws)
    put(table, "kw-notice", 1001, WWW_URL)
    real = params.get

    def throttled(name, **kw):
        if name == sweep.SWITCH_PARAM:
            raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}}, "GetParameter")
        return real(name, **kw)

    monkeypatch.setattr(params, "get", throttled)
    assert go(table, calls=(calls := [])) == {"status": "error"} and calls == []
    assert '"SweepError": 1' in capsys.readouterr().out


def test_a_failure_is_isolated_and_counted(aws, monkeypatch, capsys):
    table = aws["public"]
    keys(aws)
    put(table, "kw-notice", 1001, WWW_URL)
    real = params.get

    def throttled(name, **kw):  # the optional reserve parameter can't be read: the default holds
        if name == extract.SWEEP_RESERVE_PARAM:
            raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}}, "GetParameter")
        return real(name, **kw)

    monkeypatch.setattr(params, "get", throttled)
    assert go(table)["sweep_extracted"] == 1
    monkeypatch.setattr(sweep, "candidates", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert go(table, NIGHT + timedelta(minutes=20)) == {"status": "error"}
    out = capsys.readouterr().out
    assert '"event": "sweep_error", "error": "RuntimeError"' in out and '"SweepError": 1' in out


def test_which_notices_are_candidates():
    now = NIGHT.timestamp()
    week = int(timedelta(days=8).total_seconds())
    assert sweep.is_candidate({}, now)
    assert not sweep.is_candidate({"removed": True}, now)
    assert not sweep.is_candidate({"ttl": int(now) - 1}, now)
    assert not sweep.is_candidate({"sweeps": sweep.MAX_SWEEPS}, now)
    assert not sweep.is_candidate({"sweep_after": int(now) + 60}, now)
    assert not sweep.is_candidate({"read": {"status": "complete"}}, now)
    assert not sweep.is_candidate({"read": {"status": "partial", "last_try_at": int(now) - 3600}}, now)
    assert sweep.is_candidate({"read": {"status": "partial", "last_try_at": int(now) - week}}, now)
    assert sweep.is_candidate({"read": {"status": "failed", "last_try_at": int(now) - week}}, now)
    assert not sweep.is_candidate({"read": {"status": "partial", "last_try_at": int(now) - week,
                                            "retry_at": int(now) + 3600}}, now)


def test_the_cursor_walks_a_few_sources_a_run_and_goes_idle(aws):
    table = aws["public"]
    keys(aws)
    put(table, "kw-notice", 1001, WWW_URL)
    at = NIGHT
    go(table, at)
    assert state(table)["next"] == sweep.SOURCES_PER_RUN and set(state(table)["left"]) == set(SOURCES[:3])
    for _ in range(2):
        at += timedelta(minutes=20)
        go(table, at)
    assert set(state(table)["left"]) == set(SOURCES) and not any(state(table)["left"].values())
    queries = []
    real = table.query

    class Spy:
        def __getattr__(self, name):
            return getattr(table, name)

        def query(self, **kw):
            queries.append(1)
            return real(**kw)

    assert go(Spy(), at + timedelta(minutes=20)) == {"status": "idle"}
    assert len(queries) == 1 + sweep.IDLE_SOURCES_PER_RUN  # the queue's rows, and one source


# --- what the owner sees, and where it runs -------------------------------------------------------------------

def test_the_ops_line_says_how_many_are_left_and_a_failure_hides_nothing_else(aws, monkeypatch):
    table = aws["public"]
    assert sweep.remaining(table) is None
    table.put_item(Item={**sweep.STATE, "next": 3, "left": {"kw-notice": 5, "dept-ee": 2, "dept-archi": 0}})
    assert sweep.remaining(table) == (7, False)
    b = {"new": 3, "read": {"done": 1, "partial": 0, "pending": 2, "title": 0, "expired": 0}}
    assert opsdigest.reading_line(b, {"queue": 2, "oldest_h": 1, "sweep": sweep.remaining(table)}).endswith(
        "못 읽고 만료 0 · 다시 읽기 남음 7+")
    table.put_item(Item={**sweep.STATE, "next": 0, "left": dict.fromkeys(SOURCES, 1)})
    assert opsdigest.reading_line(b, {"queue": 0, "oldest_h": 0, "sweep": sweep.remaining(table)}).endswith("남음 6")
    monkeypatch.setattr(sweep, "remaining", lambda t: (_ for _ in ()).throw(RuntimeError("boom")))
    rq = {**opsdigest.queue_health(table, NIGHT), "sweep": "unread"}
    assert opsdigest.reading_line(b, rq).endswith("큐 0(가장 오래 0시간) · 못 읽고 만료 0 · 다시 읽기 남음 확인 못 함")


def test_the_collector_runs_the_sweep_in_its_own_phase(aws, monkeypatch):
    seen = {}
    monkeypatch.setattr(collector.params, "is_true", lambda name: True)
    monkeypatch.setattr(collector.sweep, "collect", lambda now, table, **kw: seen.setdefault("sweep", kw) and {})
    monkeypatch.setattr(collector.extract, "collect", lambda now, table, **kw: seen.setdefault("pages", kw) and {})
    monkeypatch.setattr(collector.boto3, "resource", lambda name: type("R", (), {"Table": lambda self, n: aws["public"]})())
    collector.handler({"phase": "sweep"}, None)
    assert "sweep" in seen and "pages" not in seen
    collector.handler({"phase": "pages"}, None)
    assert "sweep" not in seen["pages"]  # the daytime page runs: the queue only
    monkeypatch.setattr(collector.params, "is_true", lambda name: False)  # the collectors' own kill switch
    assert collector.handler({"phase": "sweep"}, None) == {"status": "disabled"}


def test_sweep_metrics_are_sent(aws, capsys, monkeypatch):
    table = aws["public"]
    keys(aws)
    monkeypatch.setattr(sweep, "sources", lambda: ["internship", "kw-notice"])
    put(table, "internship", 7, "https://internship.kw.ac.kr/home/notice/view.do?bbsSeq=7")
    put(table, "kw-notice", 1001, WWW_URL)
    go(table)
    out = capsys.readouterr().out
    assert '"SweepRead": 1' in out and '"SweepRecorded": 1' in out
