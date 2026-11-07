"""Every queued notice gets a read attempt and a `read` record (extract.py, notice_assets.py): coverage of
every channel, retries for images and files skipped for a reason worth another, change detection on the
assets too, 총학생회's text and poster, what readers are told, and the backfill's --incomplete."""

import asyncio
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import httpx
import pytest
import test_extract
from test_extract import (
    EE_URL,
    NOW,
    PNG,
    WWW_PAGE,
    WWW_URL,
    FakeClaude,
    Spend,
    fetcher,
    item,
    keys,
    notice,
    queue,
)

from kwcampus import extract, notice_assets, politeness, public_data, smallboards, store
from kwcampus.config import KST

isolated = test_extract.isolated  # no API key, client or spend ledger kept between tests; read_notice offline
admin = test_extract.admin

POSTER_A = "https://www.kw.ac.kr/KWData/webeditor/2026/poster-a.png"
POSTER_B = "https://www.kw.ac.kr/KWData/webeditor/2026/poster-b.png"
EMPTY_PAGE = ('<html><body><div class="board-view-box"><ul><li class="title">제목</li>'
              '<li class="contents"><p><img src="/KWData/webeditor/2026/poster-a.png"></p></li></ul></div></body></html>')
KEY = {"PK": "SRC#kw-notice", "SK": "N#1001"}


def reader(pictures, log, files=()):
    """kw_mcp.notices.read_notice's shape: the page (in hand) lists its images and files; with images=True each
    image goes through the gate, and a refusal leaves its reason on the row, as kw_mcp does."""
    from kw_mcp.notices import Answer, Picture, Refused, check_url
    from kw_mcp.text import untrusted

    async def read(url, attachment=None, images=False, part=1, *, gate=None, transport=None, limits=None):
        log.append((attachment, images))
        if attachment is not None:
            await gate.before(f"{url}&file={attachment}")
            return Answer({"text": untrusted(files[attachment - 1][1]), "total_parts": 1, "part": 1})
        await gate.before(check_url(url))
        rows = [{"index": i, "url": u, "alt": None} for i, (u, _) in enumerate(pictures, 1)]
        shown = []
        for row, (u, raw) in zip(rows, pictures if images else (), strict=False):
            try:
                await gate.before(u)
            except Refused as e:
                row["skipped"], row["skip"] = str(e), e.reason
                continue
            row["shown"] = True
            shown.append(Picture(row["index"], u, "image/png", raw))
        files_rows = [{"index": i, "name": n, "url": f"{url}&file={i}"} for i, (n, _) in enumerate(files, 1)]
        return Answer({"body": untrusted("x"), "images": rows, "attachments": files_rows}, shown)
    return read


class Clock:
    def __init__(self, at: datetime):
        self.at, self.slept = at, []

    def __call__(self) -> datetime:
        return self.at

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.at += timedelta(seconds=seconds)


def run(table, at, pages, pictures=(), log=None, claude=None, files=(), **kw):
    clock = Clock(at)
    return extract.collect(at, table, fetch=fetcher([], pages), clock=clock, sleep=clock.sleep,
                           anthropic_client=claude or FakeClaude(), read=reader(list(pictures), log if log is not None else [],
                                                                               files), **kw)


def requeue(table, key=KEY):
    """The school edited the notice: its page is queued again (store.upsert does this on an edit)."""
    store.queue_body(table, {**item(table, key), "first_seen": int(NOW.timestamp())})


def leave_spare(table, at: datetime, origin: str = "www.kw.ac.kr", spare: int = 1, cap: int | None = None) -> None:
    """The server's count for the day so that `spare` requests are left after what the list runs keep."""
    page = extract.page_for_host(origin)
    reserve = extract._reserve(page, at) if page else 0
    count = (cap if cap is not None else page.daily_cap) - spare - reserve
    table.put_item(Item={"PK": f"ORIGIN#{origin}", "SK": "STATE", "day": at.astimezone(KST).strftime("%Y-%m-%d"),
                         "day_count": count})


# --- every channel, and a record of why when there is nothing to read ------------------------------------

def test_general_and_faq_notices_are_read_now(aws):
    table = aws["public"]
    keys(aws)
    faq = WWW_URL.replace("notice.jsp", "faq.jsp")
    store.upsert(table, notice("kw-notice", 1001, "general", WWW_URL))  # 일반: skipped before
    claude = FakeClaude()
    assert run(table, NOW, {WWW_URL: WWW_PAGE}, claude=claude) == {"status": "ok", "extracted": 1}
    assert item(table, KEY)["read"]["status"] == "body_only"
    store.upsert(table, notice("kw-faq", 1001, "faq", faq))
    assert run(table, NOW + timedelta(minutes=20), {faq: WWW_PAGE}, claude=claude) == {"status": "ok", "extracted": 1}
    assert item(table, {"PK": "SRC#kw-faq", "SK": "N#1001"})["read"]["status"] == "body_only" and len(claude.calls) == 2


@pytest.mark.parametrize(("source", "url", "reason"), [
    ("internship", "https://internship.kw.ac.kr/home/notice/view.do?bbsSeq=1", "title_only_robots"),
    ("dept-media", "https://www.kwmedia.info/post/a-post", "no_reader"),
    ("dept-architecture", "https://www.kwuarchitecture.com/single-post/x", "no_reader"),
])
def test_a_notice_with_no_page_to_read_says_why_and_asks_nothing(aws, source, url, reason):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice(source, 7, f"office-{source}" if source == "internship" else source, url))
    fetched = []
    out = extract.collect(NOW, table, fetch=lambda u, **kw: fetched.append(u), anthropic_client=FakeClaude())
    assert out == {"status": "ok", "title_only": 1} and fetched == [] and not queue(table)
    read = item(table, {"PK": f"SRC#{source}", "SK": "N#7"})["read"]
    assert (read["status"], read["body"], read["last_reason"]) == ("title_only", "none", reason)


# --- what was read, and change detection on the assets too --------------------------------------------------

def test_a_poster_swap_with_the_same_text_is_read_again_and_an_untouched_notice_is_not(aws):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("kw-notice", 1001, "registration-scholarship", WWW_URL))
    claude, log = FakeClaude(), []
    assert run(table, NOW, {WWW_URL: WWW_PAGE}, [(POSTER_A, PNG)], log, claude) == {"status": "ok", "extracted": 1}
    first = item(table, KEY)["read"]
    assert first["status"] == "complete" and first["images"] == {"found": 1, "read": 1, "skipped": {}}
    assert first["files"] == {"found": 0, "read": 0, "skipped": {}} and first["tries"] == 1 and first["last_reason"] == ""
    assert first["assets_fp"] == notice_assets.fingerprint([POSTER_A], [])

    requeue(table)  # an edit that changed nothing we read: no image request, no model call
    log.clear()
    at = NOW + timedelta(minutes=20)
    assert run(table, at, {WWW_URL: WWW_PAGE}, [(POSTER_A, PNG)], log, claude) == {"status": "ok", "unchanged": 1}
    assert log == [(None, False)] and len(claude.calls) == 1 and not queue(table)

    requeue(table)  # the poster alone was swapped
    log.clear()
    at += timedelta(minutes=20)
    assert run(table, at, {WWW_URL: WWW_PAGE}, [(POSTER_B, PNG)], log, claude) == {"status": "ok", "extracted": 1}
    assert (None, True) in log and len(claude.calls) == 2
    assert [b["type"] for b in claude.calls[-1]["messages"][0]["content"]].count("image") == 1
    assert item(table, KEY)["read"]["assets_fp"] == notice_assets.fingerprint([POSTER_B], [])


def test_the_read_record_keeps_no_text_and_no_bytes(aws):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("kw-notice", 1001, "academic", WWW_URL))
    run(table, NOW, {WWW_URL: WWW_PAGE}, [(POSTER_A, PNG)], files=[("안내.pdf", "제출 기한 11월 20일, 문의 010-1111-2222")])
    stored = item(table, KEY)
    assert set(stored["read"]) == {"status", "body", "images", "files", "assets_fp", "tries", "last_try_at",
                                   "last_reason", "sent_fp"}
    flat = str(stored["read"])
    assert "11월 20일" not in flat and "010" not in flat and "PNG" not in flat and "안내.pdf" not in flat
    assert stored["read"]["files"] == {"found": 1, "read": 1, "skipped": {}}


# --- retries --------------------------------------------------------------------------------------------------

def test_skipped_images_are_tried_again_three_times_within_a_week_then_partial(aws):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("kw-notice", 1001, "academic", WWW_URL))
    claude, at = FakeClaude(), NOW
    expected_wait = [timedelta(hours=3), timedelta(days=1)]
    for attempt in (1, 2, 3):
        leave_spare(table, at)  # the page takes the last request of the day: the poster waits
        out = run(table, at, {WWW_URL: WWW_PAGE}, [(POSTER_A, PNG)], claude=claude)
        read = item(table, KEY)["read"]
        assert read["tries"] == attempt and read["images"]["skipped"] == {"budget": 1}
        if attempt < 3:
            assert out["requeued"] == 1 and read["retry_at"] == int((at + expected_wait[attempt - 1]).timestamp())
            [row] = queue(table)
            assert int(row["not_before"]) == read["retry_at"] and int(row["reads"]) == attempt
            assert int(row["ttl"]) > read["retry_at"]  # the row outlives its retry
            # not again before its time, in this run or the next ones
            assert extract.collect(at + timedelta(minutes=20), table, fetch=lambda u, **kw: pytest.fail(u),
                                   anthropic_client=claude) == {"status": "idle"}
            at = at + expected_wait[attempt - 1] + timedelta(minutes=1)
        else:
            assert "requeued" not in out and not queue(table)
            assert read["status"] == "partial" and "retry_at" not in read and read["last_reason"] == "budget"
    assert len(claude.calls) == 1  # the text once; the retries brought nothing new to show


def test_retries_stop_after_the_week_even_with_tries_left(aws):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("kw-notice", 1001, "academic", WWW_URL))
    [row] = queue(table)
    first = int((NOW - timedelta(days=8)).timestamp())
    table.update_item(Key={"PK": row["PK"], "SK": row["SK"]}, UpdateExpression="SET first_try_at = :f, #r = :t",
                      ExpressionAttributeNames={"#r": "reads"}, ExpressionAttributeValues={":f": first, ":t": 1})
    leave_spare(table, NOW)
    out = run(table, NOW, {WWW_URL: WWW_PAGE}, [(POSTER_A, PNG)])
    assert out == {"status": "ok", "extracted": 1, "assets_no_spare": 1} and not queue(table)
    assert item(table, KEY)["read"]["status"] == "partial"


def test_a_notice_with_no_text_waits_for_its_poster_before_any_model_call(aws):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("kw-notice", 1001, "academic", WWW_URL))
    claude = FakeClaude()
    leave_spare(table, NOW)
    assert run(table, NOW, {WWW_URL: EMPTY_PAGE}, [(POSTER_A, PNG)], claude=claude) == {
        "status": "ok", "requeued": 1, "assets_no_spare": 1}
    read = item(table, KEY)["read"]
    assert claude.calls == [] and read["body"] == "empty" and "retry_at" in read and "extracted" not in item(table, KEY)
    with patch("kwcampus.extract.time.time", return_value=NOW.timestamp()):  # readers use the wall clock
        assert public_data.public_view(item(table, KEY))["read"] == {"status": "pending", "missing": ["images"]}
    later = NOW + timedelta(hours=3, minutes=1)
    leave_spare(table, later, spare=5)
    assert run(table, later, {WWW_URL: EMPTY_PAGE}, [(POSTER_A, PNG)], claude=claude) == {"status": "ok", "extracted": 1}
    stored = item(table, KEY)
    assert stored["read"]["status"] == "complete" and stored["extracted"]["sources_used"] == ["images"]
    assert len(claude.calls) == 1 and "read" not in public_data.public_view(stored)


def test_a_failed_image_request_is_worth_another_try_and_a_missing_one_is_not(aws):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("kw-notice", 1001, "academic", WWW_URL))
    politeness_state = {"PK": "ORIGIN#www.kw.ac.kr", "SK": "STATE", "cooldown_until": 0}
    table.put_item(Item=politeness_state)

    def broken(pictures, log):  # the image request fails (the gate let it through, the server didn't answer)
        from kw_mcp.notices import Answer
        from kw_mcp.text import untrusted

        async def read(url, attachment=None, images=False, part=1, *, gate=None, transport=None, limits=None):
            rows = [{"index": 1, "url": POSTER_A, "alt": None}, {"index": 2, "url": POSTER_B, "alt": None}]
            if images:
                rows[0].update(skipped="no answer", skip="error")
                rows[1].update(skipped="not an image", skip="not_image")
            return Answer({"body": untrusted("x"), "images": rows, "attachments": []}, [])
        return read

    clock = Clock(NOW)
    out = extract.collect(NOW, table, fetch=fetcher([], {WWW_URL: WWW_PAGE}), clock=clock, sleep=clock.sleep,
                          anthropic_client=FakeClaude(), read=broken([], []))
    assert out == {"status": "ok", "extracted": 1, "requeued": 1}
    assert item(table, KEY)["read"]["images"]["skipped"] == {"error": 1, "not_image": 1}


# --- the page itself --------------------------------------------------------------------------------------

def test_a_page_that_keeps_failing_ends_as_failed(aws):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("dept-ee", 646, "dept-ee", EE_URL))
    for i in range(extract.MAX_TRIES):
        out = extract.collect(NOW + timedelta(minutes=20 * i), table, fetch=fetcher([], status=("http_failure", 404)),
                              anthropic_client=FakeClaude())
        assert out == {"status": "ok", "failed": 1}
    assert not queue(table)
    read = item(table, {"PK": "SRC#dept-ee", "SK": "N#646"})["read"]
    assert (read["status"], read["body"], read["last_reason"], read["tries"]) == ("failed", "failed", "http_failure", 3)


def test_a_changed_layout_is_recorded_as_failed(aws):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("dept-ee", 646, "dept-ee", EE_URL))
    out = extract.collect(NOW, table, fetch=fetcher([], {EE_URL: "<html><body>새 디자인</body></html>"}),
                          anthropic_client=FakeClaude())
    assert out == {"status": "ok", "no_body": 1}
    read = item(table, {"PK": "SRC#dept-ee", "SK": "N#646"})["read"]
    assert (read["status"], read["last_reason"]) == ("failed", "no_body")
    assert public_data.public_view(item(table, {"PK": "SRC#dept-ee", "SK": "N#646"}))["read"] == {
        "status": "failed", "missing": ["body"]}


def test_a_queued_page_counts_its_own_tries_not_an_earlier_versions(aws):
    """The queue row's tries: a notice read three times in an older version starts again at one."""
    table = aws["public"]
    keys(aws)
    key = {"PK": "SRC#dept-ee", "SK": "N#646"}
    store.upsert(table, notice("dept-ee", 646, "dept-ee", EE_URL))
    table.update_item(Key=key, UpdateExpression="SET #r = :r", ExpressionAttributeNames={"#r": "read"},
                      ExpressionAttributeValues={":r": {"status": "partial", "body": "ok", "tries": 3}})
    extract.collect(NOW, table, fetch=fetcher([], {EE_URL: "<html><body>새 디자인</body></html>"}),
                    anthropic_client=FakeClaude())
    assert item(table, key)["read"]["tries"] == 1


# --- 총학생회: its text from the list, its poster from Supabase storage -------------------------------------

COUNCIL_URL = "https://www.kwu-studentcouncil.co.kr/notices/cl1799000000000newpost1"
THUMB = "https://lqptfesfsgpdjlzwxhbt.supabase.co/storage/v1/object/public/uploads/images/1d4e0224-ca72-4d16-914a-765b6333893e.jpg"
COUNCIL_KEY = {"PK": "SRC#council", "SK": "N#cl1799000000000newpost1"}


def council_post(table, text="귀향 버스 신청: 11월 20일까지 링크트리에서 신청하세요. 문의 홍길동 010-1234-5678", thumb=THUMB):
    n = notice("council", "cl1799000000000newpost1", "office-council", COUNCIL_URL, title="동계 귀향 버스 신청")
    store.upsert(table, n)
    store.queue_extra(table, n, smallboards.council_reading({"content": text, "thumbnailUrl": thumb, "attachments": []}))


def image(data: bytes, length: bool = True) -> httpx.Response:
    """An image as the network sends it: streamed (not read ahead), with or without Content-Length."""
    headers = {"content-type": "image/png", **({"content-length": str(len(data))} if length else {})}
    return httpx.Response(200, headers=headers, content=iter([data]))


def poster_site(answers, asked):
    def handle(request):
        asked.append(str(request.url))
        return answers.pop(0) if answers else httpx.Response(404)
    return httpx.MockTransport(handle)


def test_a_council_post_is_read_from_its_list_text_and_its_poster(aws):
    table = aws["public"]
    keys(aws)
    council_post(table)
    [row] = queue(table)
    assert "010-1234-5678" not in row["text"] and "홍길동" not in row["text"]  # masked before it is kept
    asked, claude, fetched = [], FakeClaude(), []
    out = extract.collect(NOW, table, fetch=lambda u, **kw: fetched.append(u), anthropic_client=claude,
                          thumb_transport=poster_site([image(PNG)], asked))
    assert out == {"status": "ok", "extracted": 1} and fetched == [] and asked == [THUMB]
    stored = item(table, COUNCIL_KEY)
    assert stored["read"]["status"] == "complete" and stored["extracted"]["sources_used"] == ["body", "images"]
    assert not queue(table)  # the queued text is gone once read
    assert int(politeness.state(table, notice_assets.THUMB_HOST)["day_count"]) == 1
    assert "귀향" not in str(stored["read"])


@pytest.mark.parametrize(("answer", "why", "again"), [
    (httpx.Response(302, headers={"Location": "https://evil.example/x.png"}), "redirect", False),
    (image(b"\x89PNG" + b"\0" * (notice_assets.THUMB_BYTES + 10)), "too_large", False),  # said up front
    (image(b"\x89PNG" + b"\0" * (notice_assets.THUMB_BYTES + 10), length=False), "too_large", False),  # counted
    (httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>not an image</html>"), "not_image", False),
    (image(b"<html>says png, isn't</html>"), "not_image", False),
    (httpx.Response(503), "error", True),
    (httpx.Response(429), "resting", True),
])
def test_council_posters_are_capped_and_never_followed_elsewhere(aws, answer, why, again):
    table = aws["public"]
    keys(aws)
    council_post(table)
    asked = []
    out = extract.collect(NOW, table, fetch=lambda u, **kw: pytest.fail(u), anthropic_client=FakeClaude(),
                          thumb_transport=poster_site([answer], asked))
    assert out["extracted"] == 1 and asked == [THUMB] and int("requeued" in out) == int(again)
    read = item(table, COUNCIL_KEY)["read"]
    assert read["images"]["skipped"] == {why: 1} and read["status"] == ("partial" if again else "body_only")


def test_only_the_councils_own_storage_path_is_read(aws):
    assert notice_assets.thumbnail_ok(THUMB)
    for bad in (THUMB.replace("lqptfesfsgpdjlzwxhbt", "otherproject"), THUMB.replace("/public/", "/sign/"),
                THUMB + "?download=1", THUMB.replace("https:", "http:"), THUMB.replace(".co/", ".co:8443/"),
                "https://lqptfesfsgpdjlzwxhbt.supabase.co/storage/v1/object/public/uploads/images/../../x.jpg",
                "https://drive.google.com/file/d/1/view"):
        assert not notice_assets.thumbnail_ok(bad), bad
    table = aws["public"]
    keys(aws)
    council_post(table, thumb="https://drive.google.com/file/d/1lnAObNYAWh2xHdDtQi0XIMQpXTDKfie1/view")
    asked = []
    out = extract.collect(NOW, table, fetch=lambda u, **kw: pytest.fail(u), anthropic_client=FakeClaude(),
                          thumb_transport=poster_site([], asked))
    assert out == {"status": "ok", "extracted": 1} and asked == []
    assert item(table, COUNCIL_KEY)["read"]["images"] == {"found": 1, "read": 0, "skipped": {"off_host": 1}}


def test_a_council_poster_gate_has_its_own_daily_cap(aws):
    table = aws["public"]
    keys(aws)
    council_post(table)
    leave_spare(table, NOW, origin=notice_assets.THUMB_HOST, spare=0, cap=notice_assets.THUMB_DAILY_CAP)
    asked = []
    out = extract.collect(NOW, table, fetch=lambda u, **kw: pytest.fail(u), anthropic_client=FakeClaude(),
                          thumb_transport=poster_site([image(PNG)], asked))
    assert out["requeued"] == 1 and asked == []
    assert item(table, COUNCIL_KEY)["read"]["images"]["skipped"] == {"budget": 1}


# --- readers ----------------------------------------------------------------------------------------------

def test_readers_are_told_only_when_reading_was_incomplete():
    def view(read):
        return public_data.public_view({"SK": "N#1", "post_id": "1", "source": "kw-notice", "category_id": "academic",
                                        **({"read": read} if read else {})})
    assert "read" not in view(None)
    assert "read" not in view({"status": "complete", "body": "ok", "images": {"found": 6, "read": 4}})  # the cap: fine
    assert "read" not in view({"status": "body_only", "body": "ok", "images": {"found": 1, "read": 0}})  # another site's
    assert view({"status": "partial", "body": "ok", "images": {"found": 2, "read": 1},
                 "files": {"found": 1, "read": 0}})["read"] == {"status": "partial", "missing": ["images", "files"]}
    assert view({"status": "privacy_skip", "body": "ok", "images": {"found": 1, "read": 0}})["read"] == {
        "status": "privacy_skip", "missing": ["images"]}
    assert "read" not in view({"status": "privacy_skip", "body": "ok", "images": {"found": 0, "read": 0}})


# --- the backfill: notices read before reading was recorded ----------------------------------------------

def test_backfill_incomplete_records_old_reads_and_asks_claude_only_for_new_images(aws, capsys):
    table = aws["public"]
    keys(aws)
    other = WWW_URL.replace("1001", "1002")
    for duid, url in ((1001, WWW_URL), (1002, other)):  # read before reading was recorded: facts, no record
        table.put_item(Item={**notice("kw-notice", duid, "academic", url), "extract_v": extract.EXTRACT_V,
                             "extracted": {"deadlines": [], "sources_used": ["body"]}, "body_hash": "x",
                             "model": "gpt-5.4-mini",  # a page run's (they hash the page's own text)
                             "extracted_at": int(NOW.timestamp())})
    table.put_item(Item={**notice("kw-notice", 1003, "academic", WWW_URL.replace("1001", "1003")),
                         "extract_v": extract.EXTRACT_V, "extracted": {"deadlines": []},
                         "read": {"status": "complete"}})  # done: never picked
    assert admin.backfill_candidates(table, None, NOW) == []  # without --incomplete: they all have facts
    rows = admin.backfill_candidates(table, None, NOW, incomplete=True)
    assert sorted(r["SK"] for r in rows) == ["N#1001", "N#1002"]
    plan = admin.backfill_plan(table, rows, NOW)
    # each: its page, up to 4 images and 2 files, and 2 for redirects or icons (held to it by the run)
    assert plan["why"] == {"no_read_record": 2} and plan["requests_at_most"] == {"www.kw.ac.kr": 2 * (1 + 6 + 2)}
    pages = {WWW_URL: ("장학금 신청은 11월 20일까지 KLAS에서 하세요. 대상은 재학생입니다.", [b"png"], []),
             other: ("도서관 휴관 안내입니다. 11월 20일 하루 문을 닫습니다. 이용에 참고하세요.", [], [])}

    clock = Clock(NOW)
    claude = FakeClaude()
    counts = asyncio.run(admin.run_backfill(table, rows, 150, 25.0, Spend(), client=claude, now=clock,
                                            sleep=clock.sleep, read=test_extract.fake_reader(pages, [])))
    assert counts["extracted"] == 1 and counts["recorded_only"] == 1 and len(claude.calls) == 1
    assert item(table, KEY)["read"]["status"] == "complete"
    assert item(table, {"PK": "SRC#kw-notice", "SK": "N#1002"})["read"]["status"] == "body_only"
    assert admin.backfill_candidates(table, None, NOW, incomplete=True) == []


def test_backfill_dry_run_prints_a_plan_per_server(aws, monkeypatch, capsys):
    table = aws["public"]
    table.put_item(Item={**notice("kw-notice", 1001, "general", WWW_URL), "extract_v": extract.EXTRACT_V,
                         "extracted": {"deadlines": []}, "read": {"status": "partial", "tries": 1,
                                                                 "images": {"found": 3, "read": 1, "skipped": {"budget": 2}}}})
    table.put_item(Item=notice("dept-ee", 646, "dept-ee", EE_URL))
    monkeypatch.setattr(admin.boto3, "resource", lambda *a, **k: type("D", (), {"Table": lambda self, n: table})())
    monkeypatch.setattr(admin, "datetime", type("DT", (), {"now": staticmethod(lambda tz=None: NOW)}))
    monkeypatch.setattr(admin.extract, "ledger", lambda: None)
    assert admin.main(["extract-backfill", "--incomplete"]) == 0
    out = capsys.readouterr().out
    assert "candidates=2 why={'no_facts': 1, 'partial': 1}" in out
    assert "  61.111.8.4: notices=1 requests_at_most=9 spare_today=32" in out  # no record: the caps
    assert "  www.kw.ac.kr: notices=1 requests_at_most=6 spare_today=43" in out  # its page, the 3 images found, 2
    assert "dry run: nothing read or written" in out


def test_the_daily_model_cap_default_and_ssm():
    assert extract.DEFAULT_CAP == 40 and extract.CAP_PARAM == "/kw/collector/extract-daily-cap"


def test_requests_stay_within_each_servers_gate(aws):
    """Two notices on one server: one a run (the per-server turn), and the retry never lands in the same run."""
    table = aws["public"]
    keys(aws)
    other = WWW_URL.replace("1001", "1002")
    store.upsert(table, notice("kw-notice", 1001, "academic", WWW_URL))
    store.upsert(table, notice("kw-notice", 1002, "academic", other))
    leave_spare(table, NOW, spare=2)  # the page and its poster, nothing more today
    before = int(politeness.state(table, "www.kw.ac.kr")["day_count"])
    out = run(table, NOW, {WWW_URL: WWW_PAGE, other: WWW_PAGE}, [(POSTER_A, PNG)])
    assert out == {"status": "ok", "extracted": 1} and len(queue(table)) == 1  # the other one waits its turn
    assert int(politeness.state(table, "www.kw.ac.kr")["day_count"]) == before + 2
    out = run(table, NOW + timedelta(minutes=20), {WWW_URL: WWW_PAGE, other: WWW_PAGE}, [(POSTER_A, PNG)])
    assert out == {"status": "ok", "no_spare": 1} and len(queue(table)) == 1  # nothing to spare: not even its page
    assert int(politeness.state(table, "www.kw.ac.kr")["day_count"]) == before + 2
    assert date(2026, 11, 7) == NOW.astimezone(KST).date() and NOW.tzinfo is UTC
