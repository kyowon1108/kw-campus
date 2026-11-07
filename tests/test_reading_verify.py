"""The last review's findings, as tests: rows outliving their retries, retries that never came, a text edit
mid-retry, same-day deadlines, the cap on billed model calls, the read-records mark, 총동문회's date fix and the
backfill's notion of an unchanged text. (From a verifier's reproductions.)"""

from datetime import timedelta

import test_extract
from test_extract import (
    NOW,
    PNG,
    WWW_PAGE,
    WWW_URL,
    FakeClaude,
    fetcher,
    item,
    keys,
    notice,
    queue,
)
from test_reading import KEY, POSTER_A, POSTER_B, Clock, leave_spare, reader, run
from test_reading_retries import half_broken, model_error_claude
from test_smallboards import ALUMNI, ALUMNI_LIST, ALUMNI_POST, notices, site

from kwcampus import dispatcher, extract, public_data, smallboards, store

isolated = test_extract.isolated
admin = test_extract.admin


# --- the queue row and its retry ----------------------------------------------------------------------------

def test_a_malformed_answer_near_the_rows_end_keeps_the_row_past_its_retry(aws):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("kw-notice", 1001, "academic", WWW_URL))
    first = NOW + timedelta(days=13, hours=23)
    run(table, first, {WWW_URL: WWW_PAGE}, [], claude=model_error_claude("bad_output"))
    [row] = queue(table)
    rec = item(table, KEY)["read"]
    assert int(row["not_before"]) == rec["retry_at"]
    assert int(row["ttl"]) >= int(rec["retry_at"]) + int(extract.TTL_MARGIN.total_seconds())


def test_a_retry_that_never_came_reads_as_partial(aws):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("kw-notice", 1001, "academic", WWW_URL))
    leave_spare(table, NOW)
    run(table, NOW, {WWW_URL: WWW_PAGE}, [(POSTER_A, PNG)])
    stored = item(table, KEY)
    assert "retry_at" in stored["read"]
    assert extract.read_gaps(stored, NOW.timestamp()) == {"status": "pending", "missing": ["images"]}
    far = NOW + timedelta(days=40)  # the server never had a request to spare: the row ran out
    assert extract.collect(far, table, anthropic_client=FakeClaude()) == {"status": "idle"}
    assert extract.read_gaps(stored, far.timestamp()) == {"status": "partial", "missing": ["images"]}
    stale = {**stored, "read": {**stored["read"], "retry_at": 1}}  # public_view reads the clock: a long-gone retry
    assert public_data.public_view(stale)["read"] == {"status": "partial", "missing": ["images"]}


# --- a new version of the text ----------------------------------------------------------------------------------

def test_an_edit_queues_the_page_afresh(aws):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("kw-notice", 1001, "academic", WWW_URL))
    leave_spare(table, NOW)
    run(table, NOW, {WWW_URL: WWW_PAGE}, [(POSTER_A, PNG)])
    [row] = queue(table)
    assert int(row["calls"]) == 1 and int(row["reads"]) == 1
    assert store.upsert(table, notice("kw-notice", 1001, "academic", WWW_URL, title="수정된 제목 장학 안내")) == "updated"
    [row] = queue(table)
    assert not {"calls", "billed", "sent_assets", "sent_body", "reads", "not_before"} & set(row)


def test_a_text_edit_mid_retry_shows_every_asset_with_the_new_text(aws):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("kw-notice", 1001, "academic", WWW_URL))
    claude = FakeClaude()
    clock = Clock(NOW)
    extract.collect(NOW, table, fetch=fetcher([], {WWW_URL: WWW_PAGE}), clock=clock, sleep=clock.sleep,
                    anthropic_client=claude, read=half_broken())  # A read and shown; B failed
    assert len(queue(table)[0]["sent_assets"]) == 1
    edited = WWW_PAGE.replace("대상: 재학생", "대상: 대학원생 전원")  # the text changed, the list didn't
    at = NOW + timedelta(hours=3, minutes=1)
    log = []
    clock = Clock(at)
    extract.collect(at, table, fetch=fetcher([], {WWW_URL: edited}), clock=clock, sleep=clock.sleep,
                    anthropic_client=claude, read=reader([(POSTER_A, PNG), (POSTER_B, PNG)], log))
    shown = claude.calls[-1]["messages"][0]["content"]
    assert len(claude.calls) == 2 and sum(b["type"] == "image" for b in shown) == 2  # A again, with B
    stored = item(table, KEY)
    assert stored["read"]["status"] == "complete" and stored["read"]["images"]["read"] == 2
    assert stored["read"]["sent_fp"] == extract.sent_fingerprint(
        stored["body_hash"], {extract_key(POSTER_A), extract_key(POSTER_B)})


def extract_key(url: str) -> str:
    from kwcampus import notice_assets

    return notice_assets.asset_key(f"i {url}")


# --- facts ------------------------------------------------------------------------------------------------------

def test_same_day_deadlines_of_a_kind_collapse_to_the_precise_one():
    a = {"deadlines": [{"kind": "신청마감", "at": "2026-11-18", "text": "a"}], "sources_used": ["body"]}
    b = {"deadlines": [{"kind": "신청마감", "at": "2026-11-18T24:00", "text": "b"},
                       {"kind": "신청마감", "at": "2026-11-18", "text": "c"},
                       {"kind": "행사", "at": "2026-11-18", "text": "d"}], "sources_used": ["body"]}
    merged = extract.merge_facts(a, b)["deadlines"]
    assert merged == [{"kind": "행사", "at": "2026-11-18", "text": "d"},
                      {"kind": "신청마감", "at": "2026-11-18T24:00", "text": "b"}]
    assert [d["at"] for d in extract.merge_facts(b, a)["deadlines"] if d["kind"] == "신청마감"] == ["2026-11-18T24:00"]


def test_billed_model_calls_stop_at_three_a_version_failures_included(aws):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("kw-notice", 1001, "academic", WWW_URL))
    claude, at = model_error_claude("bad_output"), NOW
    for _ in range(5):
        run(table, at, {WWW_URL: WWW_PAGE}, [], claude=claude)
        at += timedelta(days=1)
    assert len(claude.calls) == extract.MAX_BILLED == 3 and not queue(table)
    rec = item(table, KEY)["read"]
    assert rec["status"] == "failed" and rec["last_reason"] == "model_bad_output" and "retry_at" not in rec


def test_a_version_with_its_answers_used_up_ends_without_another_call(aws):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("kw-notice", 1001, "academic", WWW_URL))
    [row] = queue(table)
    table.update_item(Key={"PK": row["PK"], "SK": row["SK"]}, UpdateExpression="SET billed = :b",
                      ExpressionAttributeValues={":b": extract.MAX_BILLED})
    claude = FakeClaude()
    assert run(table, NOW, {WWW_URL: WWW_PAGE}, [], claude=claude) == {"status": "ok", "model_capped": 1}
    assert claude.calls == [] and item(table, KEY)["read"]["last_reason"] == "model_calls_used"


# --- when read records began ---------------------------------------------------------------------------------------

def test_the_first_read_record_marks_when_read_records_began(aws):
    table = aws["public"]
    keys(aws)
    assert extract.reads_since(table) is None
    store.upsert(table, notice("internship", 7, "office-internship", "https://internship.kw.ac.kr/home/notice/view.do?bbsSeq=7"))
    extract.collect(NOW, table, anthropic_client=FakeClaude())
    assert extract.reads_since(table) == int(NOW.timestamp())
    extract._marked.clear()  # another container, a later read: the mark stays
    store.upsert(table, notice("kw-notice", 1001, "academic", WWW_URL))
    run(table, NOW + timedelta(days=1), {WWW_URL: WWW_PAGE}, [])
    assert extract.reads_since(table) == int(NOW.timestamp())


# --- 총동문회's guessed years -------------------------------------------------------------------------------------

def alumni_post(table, listed: str = "11-06"):
    """A new 총동문회 post listed as `listed` (its year guessed), queued for its page."""
    smallboards.collect(NOW, table, fetch=site())
    page = ALUMNI_LIST.replace("wr_id=88", "wr_id=90").replace("10-27", listed, 1)
    smallboards.collect(NOW + timedelta(hours=4), table, fetch=site(pages={ALUMNI.list_url: page}))
    return page, "https://www.kwuaa.com/bbs/board.php?bo_table=notice&wr_id=90"


def test_the_date_fix_keeps_the_hash_and_stays_put_on_the_next_list_run(aws):
    table = aws["public"]
    keys(aws)
    page, url = alumni_post(table)
    before = notices(table, "alumni")["90"]
    t = NOW + timedelta(hours=4, minutes=20)
    clock = Clock(t)
    extract.collect(t, table, fetch=fetcher([], {url: ALUMNI_POST.replace("26-08-28 14:06", "25-11-05 14:06")}),
                    clock=clock, sleep=clock.sleep, anthropic_client=FakeClaude(), read=reader([], []))
    after = notices(table, "alumni")["90"]
    assert (after["posted_date"], after["updated_date"]) == ("2025-11-05", "2025-11-05")
    assert store.item_hash(after) == after["content_hash"] != before["content_hash"]
    out = smallboards.collect(t + timedelta(hours=4), table, fetch=site(pages={ALUMNI.list_url: page}))
    assert out["alumni"]["updated"] == 0 and not queue(table)  # no loop: the list run doesn't move it back


def test_a_post_date_more_than_a_year_from_the_guess_is_not_taken(aws):
    table = aws["public"]
    keys(aws)
    _, url = alumni_post(table)
    t = NOW + timedelta(hours=4, minutes=20)
    clock = Clock(t)
    extract.collect(t, table, fetch=fetcher([], {url: ALUMNI_POST.replace("26-08-28 14:06", "24-08-28 14:06")}),
                    clock=clock, sleep=clock.sleep, anthropic_client=FakeClaude(), read=reader([], []))
    assert notices(table, "alumni")["90"]["posted_date"] == "2026-11-06"  # two years off: a misread, left alone


def test_only_our_date_fix_on_a_guessed_board_is_quiet():
    fix = dispatcher._our_date_fix
    assert dispatcher.GUESSED_DATES == frozenset({"SRC#alumni"})
    old = {"PK": "SRC#alumni", "SK": "N#90", "title": "t", "category_id": "office-alumni",
           "posted_date": "2026-11-06", "updated_date": "2026-11-06"}
    new = {**old, "posted_date": "2025-11-06", "updated_date": "2025-11-06"}
    assert fix(old, new)
    assert not fix(old, {**new, "title": "t2"})  # the school retitled it too
    assert not fix(old, {**new, "category_id": "office-council"})
    assert not fix(old, {**new, "updated_date": "2026-11-07"})  # dates no longer equal: not our shape
    assert not fix(old, {**old, "updated_date": "2026-11-07"})  # a school edit (수정일 only)
    assert not fix({**old, "PK": "SRC#dept-ee"}, {**new, "PK": "SRC#dept-ee"})  # a board with real years
    key = {"PK": "SRC#alumni", "SK": "N#90"}
    assert dispatcher._edit_job(key, old, new, "department")["quiet"] is True
    loud = dispatcher._edit_job(key, old, {**new, "title": "t2"}, "department")
    assert "quiet" not in loud and set(loud["changed"]) == {"title", "updated_date"}  # gets its 수정됨 note


# --- the backfill's "unchanged" -----------------------------------------------------------------------------------

def test_backfill_unchanged_needs_the_same_hash_or_no_edit_since_a_page_runs_hash():
    unchanged = admin._unchanged
    at = int(NOW.timestamp())
    ttl = at + int(extract_ttl().total_seconds())  # last changed at NOW
    page_run = {"body_hash": "p", "body_hash_by": "page", "extracted_at": at, "ttl": ttl}
    assert unchanged(page_run, "k")  # kw_mcp's hash can't match a page run's: no edit since is enough
    assert not unchanged({**page_run, "ttl": ttl + 3600}, "k")  # edited after its facts were stored
    reader_made = {"body_hash": "k", "body_hash_by": "reader", "extracted_at": at, "ttl": ttl}
    assert unchanged(reader_made, "k") and not unchanged(reader_made, "other")  # the same way: the hash decides
    legacy = {"body_hash": "p", "model": "gpt-5.4-mini", "extracted_at": at, "ttl": ttl}
    assert unchanged(legacy, "k")  # before body_hash_by: the page runs answered with OpenAI
    assert not unchanged({**legacy, "model": "claude-opus-5-5"}, "k")  # a backfill's: hashes compared
    assert not unchanged({"extracted_at": at, "ttl": ttl}, "k")  # no hash stored: unknown


def extract_ttl():
    from kwcampus.config import NOTICE_TTL

    return NOTICE_TTL
