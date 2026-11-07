"""Retries of a notice's images and files (extract._conclude, notice_assets): at most MAX_CALLS model calls a
notice version, only the missing assets asked for again, facts merged and never made poorer, the queue row
outliving its retry, and the edge statuses. The first tests come from a reviewer's reproductions; the last go
through the real page run and kw_mcp.notices.read_notice over a mocked network (REAL_READ: taken at import,
before test_extract's fixture swaps it out)."""

import socket
from datetime import timedelta

import httpx
import pytest
import test_extract
from kw_mcp.notices import read_notice as REAL_READ
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
from test_reading import COUNCIL_KEY, KEY, POSTER_A, POSTER_B, Clock, leave_spare, run

from kwcampus import extract, notice_assets, politeness, public_data, store

isolated = test_extract.isolated


def model_error_claude(kind):
    """A Claude client whose answer fails one way: refused, cut off, or no JSON object."""
    return {"refused": FakeClaude(stop="refusal"), "truncated": FakeClaude(stop="max_tokens"),
            "bad_output": FakeClaude(answer=["not", "an", "object"])}[kind]


def half_broken():
    """POSTER_A is read every time; POSTER_B's request fails every time (a transient error)."""
    from kw_mcp.notices import Answer, Picture, check_url
    from kw_mcp.text import untrusted

    asked = []

    async def read(url, attachment=None, images=False, part=1, *, gate=None, transport=None, limits=None):
        await gate.before(check_url(url))
        rows = [{"index": 1, "url": POSTER_A, "alt": None}, {"index": 2, "url": POSTER_B, "alt": None}]
        shown = []
        if images:
            for row in rows:
                try:
                    await gate.before(row["url"])
                except Exception as e:  # noqa: BLE001 - the gate's refusal, as kw_mcp notes it
                    row["skip"] = getattr(e, "reason", "refused")
                    continue
                asked.append(row["url"])
                if row["url"] == POSTER_A:
                    row["shown"] = True
                    shown.append(Picture(1, POSTER_A, "image/png", PNG))
                else:
                    row.update(skipped="no answer", skip="error")
        return Answer({"body": untrusted("x"), "images": rows, "attachments": []}, shown)
    read.asked = asked
    return read


def test_model_calls_per_notice_with_one_persistently_failing_image(aws):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("kw-notice", 1001, "academic", WWW_URL))
    claude, at, outs, read = FakeClaude(), NOW, [], half_broken()
    for wait in (timedelta(0), timedelta(hours=3, minutes=1), timedelta(days=1, minutes=1)):
        at += wait
        clock = Clock(at)
        outs.append(extract.collect(at, table, fetch=fetcher([], {WWW_URL: WWW_PAGE}), clock=clock, sleep=clock.sleep,
                                    anthropic_client=claude, read=read))
    rec = item(table, KEY)["read"]
    assert len(claude.calls) == 1  # the first read; the retries brought nothing the model hadn't seen
    assert read.asked == [POSTER_A, POSTER_B, POSTER_B, POSTER_B]  # A once, then only the one still missing
    assert rec["status"] == "partial" and "retry_at" not in rec and rec["tries"] == 3 and not queue(table)
    assert rec["images"] == {"found": 2, "read": 1, "skipped": {"error": 1}}
    assert rec["sent_fp"] == extract.sent_fingerprint(item(table, KEY)["body_hash"],
                                                      {notice_assets.asset_key(f"i {POSTER_A}")})


def test_a_retry_planned_past_the_queue_rows_ttl_keeps_the_row_alive(aws):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("kw-notice", 1001, "academic", WWW_URL))
    first = NOW + timedelta(days=13, hours=23)  # the server had nothing to spare until now
    leave_spare(table, first)
    out = run(table, first, {WWW_URL: WWW_PAGE}, [(POSTER_A, PNG)])
    assert out["requeued"] == 1
    rec = item(table, KEY)["read"]
    [row] = queue(table)
    assert rec["retry_at"] < int(row["ttl"]) and int(row["ttl"]) >= rec["retry_at"] + 86400  # a day past its retry
    later = first + timedelta(hours=3, minutes=1)
    leave_spare(table, later, spare=5)
    out = run(table, later, {WWW_URL: WWW_PAGE}, [(POSTER_A, PNG)])
    assert out == {"status": "ok", "extracted": 1} and not queue(table)  # read, not left pending
    assert item(table, KEY)["read"]["status"] == "complete"
    assert "read" not in public_data.public_view(item(table, KEY))


def test_a_retry_past_the_week_is_no_retry(aws):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("kw-notice", 1001, "academic", WWW_URL))
    [row] = queue(table)
    first = int((NOW - timedelta(days=6, hours=22)).timestamp())  # 3 hours from now is past the week
    table.update_item(Key={"PK": row["PK"], "SK": row["SK"]}, UpdateExpression="SET first_try_at = :f",
                      ExpressionAttributeValues={":f": first})
    leave_spare(table, NOW)
    assert "requeued" not in run(table, NOW, {WWW_URL: WWW_PAGE}, [(POSTER_A, PNG)])
    assert item(table, KEY)["read"]["status"] == "partial" and not queue(table)


# --- model failures ---------------------------------------------------------------------------------------

@pytest.mark.parametrize(("kind", "outcome"), [("refused", "refused"), ("truncated", "truncated")])
def test_a_refused_or_cut_answer_ends_the_retries(aws, kind, outcome):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("kw-notice", 1001, "academic", WWW_URL))
    leave_spare(table, NOW)  # images skipped for budget: a retry would be planned
    out = run(table, NOW, {WWW_URL: WWW_PAGE}, [(POSTER_A, PNG)], claude=model_error_claude(kind))
    assert out[outcome] == 1 and not queue(table)
    rec = item(table, KEY)["read"]
    assert "retry_at" not in rec and rec["status"] == "failed" and rec["last_reason"] == f"model_{kind}"


def test_a_malformed_answer_waits_before_another_try(aws):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("kw-notice", 1001, "academic", WWW_URL))
    out = run(table, NOW, {WWW_URL: WWW_PAGE}, [], claude=model_error_claude("bad_output"))
    assert out == {"status": "ok", "model_failed": 1, "requeued": 1}
    [row] = queue(table)
    rec = item(table, KEY)["read"]
    assert int(row["tries"]) == 1 and int(row["not_before"]) == rec["retry_at"] > int(NOW.timestamp())
    assert rec["status"] == "failed" and rec["last_reason"] == "model_bad_output"
    assert extract.collect(NOW + timedelta(minutes=20), table, anthropic_client=FakeClaude()) == {"status": "idle"}
    later = NOW + timedelta(hours=3, minutes=1)
    assert run(table, later, {WWW_URL: WWW_PAGE}, []) == {"status": "ok", "extracted": 1}
    assert "retry_at" not in item(table, KEY)["read"] and not queue(table)


# --- facts are merged and never made poorer --------------------------------------------------------------------

def test_facts_of_a_fuller_read_are_never_replaced_by_a_poorer_one(aws):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("kw-notice", 1001, "academic", WWW_URL))
    claude = FakeClaude()
    run(table, NOW, {WWW_URL: WWW_PAGE}, [(POSTER_A, PNG)], claude=claude)
    full = item(table, KEY)["extracted"]
    assert full["sources_used"] == ["body", "images"]
    test_extract_requeue(table)
    leave_spare(table, NOW + timedelta(minutes=20))  # an edit swapped the poster, which can't be read this time
    out = run(table, NOW + timedelta(minutes=20), {WWW_URL: WWW_PAGE}, [(POSTER_B, PNG)], claude=claude)
    assert out["requeued"] == 1 and len(claude.calls) == 1  # waits for the poster instead of a body-only call
    assert item(table, KEY)["extracted"] == full


def test_merge_keeps_what_either_read_found():
    old = {"deadlines": [{"kind": "신청마감", "at": "2026-11-20", "text": "a"}], "target": "재학생", "amount": "",
           "eligibility": ["12학점"], "category_hint": "기타", "confidence": 0.5, "sources_used": ["body", "images"],
           "how_to_apply": "", "contact_office": "", "summary": "요약"}
    new = {"deadlines": [{"kind": "행사", "at": "2026-11-25", "text": "b"}], "target": "", "amount": "50만원",
           "eligibility": ["12학점", "직전학기"], "category_hint": "장학", "confidence": 0.9,
           "sources_used": ["body", "attachments"], "how_to_apply": "KLAS", "contact_office": "", "summary": "다른 요약"}
    merged = extract.merge_facts(old, new)
    assert [d["at"] for d in merged["deadlines"]] == ["2026-11-20", "2026-11-25"]
    assert (merged["target"], merged["amount"], merged["how_to_apply"], merged["summary"]) == ("재학생", "50만원", "KLAS", "요약")
    assert merged["eligibility"] == ["12학점", "직전학기"] and merged["category_hint"] == "장학"
    assert float(merged["confidence"]) == 0.9 and merged["sources_used"] == ["body", "images", "attachments"]


def test_extract_requeue(table, key=KEY):
    """(helper) the school edited the notice: its page is queued again."""
    if table is None:
        return
    store.queue_body(table, {**item(table, key), "first_seen": int(NOW.timestamp())})


test_extract_requeue.__test__ = False


# --- edge statuses and the first pass -------------------------------------------------------------------------

def test_a_council_post_whose_title_may_name_people_is_a_privacy_skip_even_without_a_poster(aws):
    table = aws["public"]
    keys(aws)
    n = notice("council", "cl1799000000000newpost1", "office-council",
               "https://www.kwu-studentcouncil.co.kr/notices/cl1799000000000newpost1", title="홍*동 학우 수상 축하")
    store.upsert(table, n)
    store.queue_extra(table, n, {"text": "축하합니다. 이번 대회에서 큰 상을 받았습니다. 모두 축하해 주세요.",
                                 "thumb": "", "other_images": 0, "links": 0})
    extract.collect(NOW, table, fetch=lambda u, **kw: pytest.fail(u), anthropic_client=FakeClaude())
    assert item(table, COUNCIL_KEY)["read"]["status"] == "privacy_skip"


def test_a_listing_that_fails_is_a_failed_read_and_an_unknown_host_says_so(aws, monkeypatch):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("kw-notice", 1001, "academic", WWW_URL))

    async def broken(url, **kw):
        raise RuntimeError("parser crashed")

    clock = Clock(NOW)
    extract.collect(NOW, table, fetch=fetcher([], {WWW_URL: WWW_PAGE}), clock=clock, sleep=clock.sleep,
                    anthropic_client=FakeClaude(), read=broken)
    rec = item(table, KEY)["read"]
    assert (rec["status"], rec["last_reason"]) == ("failed", "list_failed") and "retry_at" not in rec

    from kw_mcp import notices

    def unknown(url, **kw):
        raise notices.NoticeError("unknown host")

    monkeypatch.setattr(notices, "check_url", unknown)
    test_extract_requeue(table)
    extract.collect(NOW + timedelta(minutes=20), table, fetch=fetcher([], {WWW_URL: WWW_PAGE.replace("31", "30")}),
                    anthropic_client=FakeClaude())
    rec = item(table, KEY)["read"]
    assert (rec["status"], rec["last_reason"]) == ("partial", "host_unknown") and "retry_at" not in rec


def test_title_only_records_are_written_before_any_stop(aws):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("internship", 7, "office-internship", "https://internship.kw.ac.kr/home/notice/view.do?bbsSeq=7"))
    store.upsert(table, notice("kw-notice", 1001, "academic", WWW_URL))
    out = extract.collect(NOW, table, fetch=lambda u, **kw: pytest.fail(u), anthropic_client=FakeClaude(), cap=0)
    assert out == {"status": "ok", "title_only": 1, "skipped_cap": 1}
    assert item(table, {"PK": "SRC#internship", "SK": "N#7"})["read"]["status"] == "title_only"
    assert len(queue(table)) == 1  # the readable one waits for tomorrow's model calls


def test_a_row_gone_meanwhile_is_no_error(aws):
    table = aws["public"]
    row = {"PK": "BODYQ", "SK": "SRC#kw-notice|N#1"}
    assert extract._failed(table, row, "http_failure", NOW, NOW + timedelta(hours=3)) == (1, False)
    assert extract._requeue(table, row, 1, int(NOW.timestamp()), NOW + timedelta(hours=3), set(), 0, "") is False
    assert table.scan()["Items"] == []  # nothing created by the updates


def test_name_lookups_are_refused_in_tests(no_network):
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("example.com", 443)
    assert socket.getaddrinfo("localhost", 80)
    assert no_network == ["lookup example.com"]
    no_network.clear()


# --- through the real page run and kw_mcp.notices.read_notice -----------------------------------------------

POSTER_C = "https://www.kw.ac.kr/KWData/webeditor/2026/poster-c.png"
LIVE_PAGE = ('<html><body><div class="board-view-box"><ul><li class="title">2026학년도 장학생 선발 안내</li>'
             '<li class="contents"><p>신청기간: 2026.11.10.(화) ~ 11.18.(수) 24:00, 대상: 재학생, KLAS에서 신청</p>'
             f'<p><img src="{POSTER_A}"></p><p><img src="{POSTER_B}"></p><p><img src="{POSTER_C}"></p>'
             '</li></ul></div></body></html>')


class Site:
    """The school's images over a mocked network: per URL, the status each request gets (the last repeats)."""

    def __init__(self, plan):
        self.plan, self.asked = plan, []

    def __call__(self, request):
        url = str(request.url)
        self.asked.append(url)
        statuses = self.plan[url]
        status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
        if status == 200:
            return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)
        return httpx.Response(status)


def live(table, at, site, claude):
    from kw_mcp import notices

    notices.clear_cache()
    clock = Clock(at)
    return extract.collect(at, table, fetch=fetcher([], {WWW_URL: LIVE_PAGE}), clock=clock, sleep=clock.sleep,
                           anthropic_client=claude, read=REAL_READ,
                           inner_transport=lambda: httpx.MockTransport(site))


def test_end_to_end_only_missing_images_are_asked_again_and_at_most_two_model_calls(aws):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("kw-notice", 1001, "registration-scholarship", WWW_URL))
    site = Site({POSTER_A: [200], POSTER_B: [404, 200], POSTER_C: [404]})  # B comes back on the retry, C never
    claude = FakeClaude()
    assert live(table, NOW, site, claude)["requeued"] == 1
    assert site.asked == [POSTER_A, POSTER_B, POSTER_C]
    first = item(table, KEY)
    assert first["read"]["images"] == {"found": 3, "read": 1, "skipped": {"error": 2}}
    assert len(claude.calls) == 1 and first["extracted"]["sources_used"] == ["body", "images"]

    site.asked.clear()
    out = live(table, NOW + timedelta(hours=3, minutes=1), site, claude)
    assert site.asked == [POSTER_B, POSTER_C]  # A is not asked for again
    assert out == {"status": "ok", "extracted": 1} and len(claude.calls) == 2  # B is new: the second and last call
    assert [b["type"] for b in claude.calls[-1]["messages"][0]["content"]].count("image") == 1  # B alone
    after = item(table, KEY)
    assert after["read"]["status"] == "partial" and "retry_at" not in after["read"] and not queue(table)
    assert after["read"]["images"] == {"found": 3, "read": 2, "skipped": {"error": 1}}
    assert int(politeness.state(table, "www.kw.ac.kr")["day_count"]) == 2 + 3 + 2  # 2 pages, 3 then 2 images
    assert public_data.public_view(after)["read"] == {"status": "partial", "missing": ["images"]}


def test_end_to_end_a_notice_whose_images_all_arrive_is_complete_after_one_retry(aws):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("kw-notice", 1001, "registration-scholarship", WWW_URL))
    site = Site({POSTER_A: [200], POSTER_B: [503, 200], POSTER_C: [200]})
    claude = FakeClaude()
    live(table, NOW, site, claude)
    site.asked.clear()
    live(table, NOW + timedelta(hours=3, minutes=1), site, claude)
    assert site.asked == [POSTER_B]
    rec = item(table, KEY)["read"]
    assert rec["status"] == "complete" and rec["images"]["read"] == 3 and len(claude.calls) == 2


def test_backfill_asks_claude_only_for_sources_its_facts_lacked(aws):
    """--incomplete on a notice whose facts already came from its text and poster: the poster read again is no
    reason to ask Claude again (its text unchanged since): only the read record is written."""
    import asyncio

    table = aws["public"]
    keys(aws)
    admin = test_extract.admin
    table.put_item(Item={**notice("kw-notice", 1001, "academic", WWW_URL), "extract_v": extract.EXTRACT_V,
                         "extracted": {"deadlines": [], "sources_used": ["body", "images"]}, "body_hash": "page-run",
                         "body_hash_by": "page", "extracted_at": int(NOW.timestamp())})
    rows = admin.backfill_candidates(table, None, NOW, incomplete=True)
    pages = {WWW_URL: ("장학금 신청은 11월 20일까지 KLAS에서 하세요. 대상은 재학생입니다.", [b"png"], [])}
    clock = Clock(NOW)
    claude = FakeClaude()
    counts = asyncio.run(admin.run_backfill(table, rows, 150, 25.0, test_extract.Spend(), client=claude, now=clock,
                                            sleep=clock.sleep, read=test_extract.fake_reader(pages, [])))
    assert counts["recorded_only"] == 1 and claude.calls == []
    assert item(table, KEY)["read"]["status"] == "complete"
    # edited after its facts were stored (store.upsert moved its ttl): the text may differ, so Claude reads it
    table.update_item(Key=KEY, UpdateExpression="SET #t = :t, #r = :r",
                      ExpressionAttributeNames={"#t": "ttl", "#r": "read"},
                      ExpressionAttributeValues={":t": int((NOW + timedelta(days=121)).timestamp()),
                                                 ":r": {"status": "partial"}})
    rows = admin.backfill_candidates(table, None, NOW, incomplete=True)
    asyncio.run(admin.run_backfill(table, rows, 150, 25.0, test_extract.Spend(), client=claude, now=clock,
                                   sleep=clock.sleep, read=test_extract.fake_reader(pages, [])))
    assert len(claude.calls) == 1


def test_a_poster_address_with_credentials_is_refused():
    thumb = "https://lqptfesfsgpdjlzwxhbt.supabase.co/storage/v1/object/public/uploads/images/1d4e0224-ca72-4d16-914a-765b6333893e.jpg"
    assert notice_assets.thumbnail_ok(thumb)
    for bad in (thumb.replace("https://", "https://user@"), thumb.replace("https://", "https://:secret@"),
                thumb.replace("https://", "https://user:secret@")):
        assert not notice_assets.thumbnail_ok(bad), bad


def test_a_final_poorer_read_merges_instead_of_replacing(aws):
    """The last try still lacks the poster the facts came from: the model reads what there is (a new file), and
    what the fuller read found stays."""
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("kw-notice", 1001, "academic", WWW_URL))
    first = FakeClaude(answer={**test_extract.ANSWER, "amount": "50만원"})
    run(table, NOW, {WWW_URL: WWW_PAGE}, [(POSTER_A, PNG)], claude=first,
        files=[("안내.pdf", "제출 서류: 신청서, 성적증명서")])
    assert item(table, KEY)["extracted"]["sources_used"] == ["body", "images", "attachments"]
    test_extract_requeue(table)
    row = queue(table)[0]
    table.update_item(Key={"PK": row["PK"], "SK": row["SK"]}, UpdateExpression="SET #r = :r",
                      ExpressionAttributeNames={"#r": "reads"}, ExpressionAttributeValues={":r": 2})  # its last try
    from kw_mcp.notices import Answer, check_url
    from kw_mcp.text import untrusted

    async def poster_fails(url, attachment=None, images=False, part=1, *, gate=None, transport=None, limits=None):
        if attachment is not None:  # the new form reads fine
            await gate.before(f"{url}&file=1")
            return Answer({"text": untrusted("새 양식: 12월 1일까지 제출"), "total_parts": 1, "part": 1})
        await gate.before(check_url(url))
        row = {"index": 1, "url": POSTER_B, "alt": None}
        if images:
            row.update(skipped="no answer", skip="error")  # the new poster doesn't come
        return Answer({"body": untrusted("x"), "images": [row],
                       "attachments": [{"index": 1, "name": "양식.pdf", "url": f"{url}&file=1"}]}, [])

    second = FakeClaude(answer={**test_extract.ANSWER, "amount": ""})
    clock = Clock(NOW + timedelta(minutes=20))
    extract.collect(clock(), table, fetch=fetcher([], {WWW_URL: WWW_PAGE}), clock=clock, sleep=clock.sleep,
                    anthropic_client=second, read=poster_fails)
    assert len(second.calls) == 1  # the new form is worth a call
    facts = item(table, KEY)["extracted"]
    assert facts["amount"] == "50만원" and facts["sources_used"] == ["body", "images", "attachments"]
    assert item(table, KEY)["read"]["status"] == "partial" and not queue(table)
