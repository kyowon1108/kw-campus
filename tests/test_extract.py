"""Notice pages read once (extract.py): the queue, the readers, the model call (stubbed), the strict
check, what readers get (public_data / public_http), the collector's page runs and the backfill."""

import asyncio
import base64
import hashlib
import importlib.util
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import httpx2
import pytest
from boto3.dynamodb.types import TypeSerializer
from kw_mcp.notices import read_notice as REAL_READ
from starlette.testclient import TestClient

from kwcampus import (
    collector,
    dispatcher,
    extract,
    politeness,
    public_data,
    public_http,
    store,
)
from kwcampus.fetch import FetchResult
from kwcampus.kwnotice.domain import CategoryId, CategoryName, NoticeSummary
from kwcampus.kwnotice.redaction import redact_body
from kwcampus.kwnotice.values import DUID, SourceURL

_spec = importlib.util.spec_from_file_location("collector_admin",
                                               Path(__file__).parents[1] / "scripts" / "collector_admin.py")
admin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(admin)

NOW = datetime(2026, 11, 7, 1, 10, tzinfo=UTC)  # Saturday 10:10 KST: no list runs left today
WEEKDAY = datetime(2026, 11, 5, 1, 10, tzinfo=UTC)  # Thursday 10:10 KST
CLAUDE_KEY = "sk-ant-test-" + "a" * 24
OPENAI_KEY = "sk-test-" + "b" * 24
WWW_URL = "https://www.kw.ac.kr/ko/life/notice.jsp?BoardMode=view&DUID=1001"
EE_URL = "https://ee.kw.ac.kr/HTML/department/undergrad_info.php?BoardMode=view&UID=646"
AICON_URL = "https://aicon.kw.ac.kr/administration/notice.php?admin_mode=read&no=3514"

WWW_PAGE = """<html><body><div class="board-view-box"><ul><li class="title">제목 줄</li>
<li class="contents"><p><span>2026</span><span>학년도 </span><span>2학기 장학생 선발 안내</span></p>
<p>신청기간: 2026.11.10.(화) ~ 11.18.(수) 24:00</p><p>대상: 재학생</p>
<p>신청: KLAS &gt; 장학 &gt; 장학금신청<br>링크: https://forms.example.com/apply</p>
<p>문의: 학생복지팀 홍길동 (02-940-5114, hong@kw.ac.kr)</p><p><img alt="포스터: 장학 안내" src="/x.png"></p>
</li></ul></div></body></html>"""
EE_PAGE = '<div class="view_con"><p>졸업작품 최종보고서를 2026년 11월 20일까지 학과 사무실로 제출하세요.</p></div>'
AICON_PAGE = ('<table class="board-view"><tr><td class="view_td"><p>__접수기간<br/>2026.11.03(화) ~ 2026.11.18(수) 23:59'
              '</p><ul><li>학력 및 전공 무관</li></ul></td></tr></table>')
PAGES = {WWW_URL: WWW_PAGE, EE_URL: EE_PAGE, AICON_URL: AICON_PAGE}

ANSWER = {
    "deadlines": [{"kind": "신청마감", "at": "2026-11-18T24:00", "text": "11.18.(수) 24:00까지"},
                  {"kind": "행사", "at": "2031-01-01", "text": "먼 미래"}],
    "target": "재학생", "eligibility": ["직전학기 12학점 이상"], "how_to_apply": "KLAS > 장학 > 장학금신청",
    "amount": "", "contact_office": "학생복지팀", "category_hint": "장학",
    "summary": "재학생 대상 2학기 장학생 선발, 11/18까지 KLAS 신청", "confidence": 0.9,
}


async def _no_assets(url, attachment=None, images=False, part=1, *, gate=None, transport=None, limits=None):
    """A page with no images or files: page runs that don't pass `read` never reach the network."""
    from kw_mcp.notices import Answer

    return Answer({"body": "", "images": [], "attachments": []})


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """No key, client or ledger kept between tests, and never the owner's real spend ledger."""
    from kw_mcp import notices

    monkeypatch.setattr(notices, "read_notice", _no_assets)
    notices.clear_cache()
    monkeypatch.setenv("KW_SPEND_LEDGER", str(tmp_path / "no-ledger.py"))
    extract._keys.clear()
    extract._anthropic_clients.clear()
    extract._ledgers.clear()
    public_data.clear_cache()
    yield
    extract._keys.clear()
    extract._anthropic_clients.clear()
    extract._ledgers.clear()


def keys(aws, claude=True, openai=True):
    if claude:
        aws["ssm"].put_parameter(Name=extract.KEY_PARAMS["anthropic"], Value=CLAUDE_KEY, Type="SecureString")
    if openai:
        aws["ssm"].put_parameter(Name=extract.KEY_PARAMS["openai"], Value=OPENAI_KEY, Type="SecureString")


def notice(source, duid, category, url, title="2026학년도 2학기 장학생 선발 안내", posted=date(2026, 11, 6), seed=False,
           when=NOW):
    n = NoticeSummary(duid=DUID(str(duid)), title=title, category_id=CategoryId(category),
                      category_name=CategoryName("x"), posted_date=posted, updated_date=posted, department="학생복지팀",
                      source_url=SourceURL(url), attachments_present=False, pinned=False)
    return store.notice_item(source, n, when, seed=seed)


def queue(table):
    return table.query(KeyConditionExpression="PK = :q", ExpressionAttributeValues={":q": store.BODY_QUEUE})["Items"]


def item(table, key):
    return table.get_item(Key={"PK": key["PK"], "SK": key["SK"]})["Item"]


def fetcher(calls, pages=PAGES, status=None):
    def fetch(url, encoding="utf-8", errors="strict"):
        calls.append(url)
        if status:
            return FetchResult(None, status[0], status[1], status[2] if len(status) > 2 else 0)
        return FetchResult(pages[url], None, 200)
    return fetch


class FakeClaude:
    def __init__(self, answer=ANSWER, error=None, stop="end_turn", model="claude-opus-5-5"):
        self.answer, self.error, self.stop, self.model, self.calls = answer, error, stop, model, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(stop_reason=self.stop, model=self.model,
                               content=[SimpleNamespace(type="text", text=json.dumps(self.answer, ensure_ascii=False))],
                               usage=SimpleNamespace(input_tokens=1200, output_tokens=300))


def openai_answer(answer=ANSWER, status="completed"):
    calls = []

    def send(payload, key):
        calls.append((payload, key))
        return {"status": status, "model": "gpt-5.4-mini", "usage": {"input_tokens": 900, "output_tokens": 250},
                "output": [{"type": "reasoning"}, {"type": "message", "content": [
                    {"type": "output_text", "text": json.dumps(answer, ensure_ascii=False)}]}]}
    send.calls = calls
    return send


def api_error(cls, status):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("x", response=httpx2.Response(status, request=request), body=None)


_ser = TypeSerializer()


def modify(old, new):
    return {"eventName": "MODIFY", "dynamodb": {"OldImage": {k: _ser.serialize(v) for k, v in old.items()},
                                                "NewImage": {k: _ser.serialize(v) for k, v in new.items()}}}


# --- the queue -----------------------------------------------------------------------------------

def test_new_and_retitled_notices_are_queued_but_seeds_and_our_own_retitles_are_not(aws):
    table = aws["public"]
    store.upsert(table, notice("kw-notice", 1, "academic", WWW_URL, seed=True))
    assert queue(table) == []
    first = notice("kw-notice", 2, "academic", WWW_URL)
    assert store.upsert(table, first) == "inserted"
    assert [r["SK"] for r in queue(table)] == ["SRC#kw-notice|N#2"]
    table.delete_item(Key={"PK": store.BODY_QUEUE, "SK": "SRC#kw-notice|N#2"})
    assert store.upsert(table, {**first, "pinned": True, "content_hash": "p"}) == "updated"  # pinned only
    assert queue(table) == []
    assert store.upsert(table, {**first, "title": "제목 바뀜", "content_hash": "t"}) == "updated"
    assert [r["SK"] for r in queue(table)] == ["SRC#kw-notice|N#2"]
    table.delete_item(Key={"PK": store.BODY_QUEUE, "SK": "SRC#kw-notice|N#2"})
    # our redaction changed the title (norm_v up, same 수정일): no new page
    assert store.upsert(table, {**first, "title": "가린 제목", "content_hash": "r", "norm_v": 99}) == "updated"
    assert queue(table) == []
    # the school edited it: the same title, a new 수정일 on the list
    assert store.upsert(table, {**first, "title": "가린 제목", "updated_date": "2026-11-07", "content_hash": "e",
                                "norm_v": 99}) == "updated"
    assert [r["SK"] for r in queue(table)] == ["SRC#kw-notice|N#2"]


def test_an_edited_notice_is_read_again_and_its_facts_follow_the_edit(aws):
    table = aws["public"]
    keys(aws)
    first = notice("kw-notice", 1001, "registration-scholarship", WWW_URL)
    store.upsert(table, first)
    claude = FakeClaude()
    assert extract.collect(NOW, table, fetch=fetcher([], {WWW_URL: WWW_PAGE}), anthropic_client=claude) == {
        "status": "ok", "extracted": 1}
    key = {"PK": first["PK"], "SK": first["SK"]}
    assert item(table, key)["extracted"]["deadlines"][0]["at"] == "2026-11-18T23:59:59+09:00"

    # the school moves the deadline: the list shows a new 수정일, the page a new date
    edited = {**first, "updated_date": "2026-11-08", "content_hash": "edited"}
    assert store.upsert(table, edited) == "updated" and len(queue(table)) == 1
    page = WWW_PAGE.replace("11.18.(수) 24:00", "11.25.(수) 24:00")
    moved = {**ANSWER, "deadlines": [{"kind": "신청마감", "at": "2026-11-25T24:00", "text": "11.25.(수) 24:00까지"}]}
    later, calls = FakeClaude(answer=moved), []
    out = extract.collect(NOW + timedelta(minutes=20), table, fetch=fetcher(calls, {WWW_URL: page}), anthropic_client=later)
    assert out == {"status": "ok", "extracted": 1} and calls == [WWW_URL] and len(later.calls) == 1
    assert item(table, key)["extracted"]["deadlines"][0]["at"] == "2026-11-25T23:59:59+09:00"
    assert queue(table) == []

    # edited again, but only the 수정일 moved: one page request, no model call, the facts stay
    store.upsert(table, {**edited, "updated_date": "2026-11-09", "content_hash": "again"})
    unchanged, calls = FakeClaude(), []
    out = extract.collect(NOW + timedelta(minutes=40), table, fetch=fetcher(calls, {WWW_URL: page}),
                          anthropic_client=unchanged)
    assert out == {"status": "ok", "unchanged": 1} and calls == [WWW_URL] and unchanged.calls == []
    assert item(table, key)["extracted"]["deadlines"][0]["at"] == "2026-11-25T23:59:59+09:00" and queue(table) == []


# --- reading pages -----------------------------------------------------------------------------

def test_each_board_layout_gives_its_main_text():
    www = extract.body_text(WWW_PAGE, extract._PAGES["kw-notice"].selector)
    assert www.splitlines()[:2] == ["2026학년도 2학기 장학생 선발 안내", "신청기간: 2026.11.10.(화) ~ 11.18.(수) 24:00"]
    assert "제목 줄" not in www and "포스터: 장학 안내" in www  # the title row is not body; alt texts are
    assert extract.body_text(EE_PAGE, extract._PAGES["dept-ee"].selector).startswith("졸업작품 최종보고서")
    assert extract.body_text(AICON_PAGE, extract._PAGES["aicon"].selector).splitlines() == [
        "__접수기간", "2026.11.03(화) ~ 2026.11.18(수) 23:59", "학력 및 전공 무관"]
    assert extract.body_text("<html><body>바뀐 화면</body></html>", ".view_con") is None
    assert extract.page_for({"source": "dept-ee", "url": "https://evil.example.com/x"}) is None
    assert not extract.wanted({"source": "kw-notice", "category_id": "general", "url": WWW_URL})  # 일반: not read
    assert not extract.wanted({"source": "kw-faq", "category_id": "faq", "url": WWW_URL})
    assert extract.wanted({"source": "dept-ee", "category_id": "dept-ee", "url": EE_URL})


def test_bodies_lose_contact_people_phones_and_addresses():
    assert redact_body("문의: 학생복지팀 홍길동 (02-940-5114, hong@kw.ac.kr)") == (
        "문의: 학생복지팀 [REDACTED_NAME] ([REDACTED_PHONE], [REDACTED_EMAIL])")
    assert redact_body("담당자 김철수 주무관\n신청: 이메일로 제출") == "담당자 [REDACTED_NAME] 주무관\n신청: 이메일로 제출"
    assert redact_body("박영희(010-1234-5678)") == "[REDACTED_NAME]([REDACTED_PHONE])"
    assert redact_body("문의사항은 학생복지팀으로 연락 바랍니다") == "문의사항은 학생복지팀으로 연락 바랍니다"


# --- the strict check ----------------------------------------------------------------------------

ITEM = {"posted_date": "2026-11-06", "updated_date": "2026-11-06"}


def test_validate_keeps_only_believable_dates_and_no_personal_data():
    original = extract.body_text(WWW_PAGE, extract._PAGES["kw-notice"].selector)
    sent = redact_body(original)
    raw = {**ANSWER,
           "deadlines": [*ANSWER["deadlines"], {"kind": "신청마감", "at": "2026-02-30", "text": "x"},
                         {"kind": "마감", "at": "2026-11-20", "text": "모르는 종류"},
                         {"kind": "서류마감", "at": "2026-11-19T17:00", "text": "홍길동 교수에게 제출"},
                         {"kind": "신청마감", "at": "2026-11-18T24:00", "text": "중복"}],
           "eligibility": ["직전학기 12학점 이상", "홍길동 학생 추천", "a" * 100],
           "how_to_apply": "https://phish.example.net/x 에서 신청", "amount": "1인 100만원",
           "contact_office": "학생복지팀 김철수", "confidence": 3, "category_hint": "없는값",
           "summary": "재학생 대상 장학생 선발 안내, 11/18까지 신청 " + "가" * 80}
    facts, dropped = extract.validate(raw, ITEM, sent, original, ("body", "images"))
    assert facts["deadlines"] == [
        {"kind": "신청마감", "at": "2026-11-18T23:59:59+09:00", "text": "11.18.(수) 24:00까지"},
        {"kind": "서류마감", "at": "2026-11-19T17:00:00+09:00", "text": ""},  # a name in the text: text dropped
    ]
    assert facts["eligibility"] == ["직전학기 12학점 이상", "a" * 59 + "…"]
    assert facts["how_to_apply"] == "" and facts["amount"] == "1인 100만원"  # a link the page never had
    assert facts["contact_office"] == ""  # a person next to the office
    assert facts["confidence"] == 1 and facts["category_hint"] == "기타"
    assert len(facts["summary"]) == extract.SUMMARY_CHARS - 1 and facts["sources_used"] == ["body", "images"]
    assert dropped >= 5

    good, _ = extract.validate(ANSWER, ITEM, sent, original)
    # the office's own 02-940 number, printed on its line on the page, comes along; never the person
    assert good["contact_office"] == "학생복지팀 (02-940-5114)" and good["sources_used"] == ["body"]
    mobile = original.replace("02-940-5114", "010-1234-5678")
    assert extract.validate(ANSWER, ITEM, redact_body(mobile), mobile)[0]["contact_office"] == "학생복지팀"
    linked = {**ANSWER, "how_to_apply": "https://forms.example.com/apply"}
    assert extract.validate(linked, ITEM, sent, original)[0]["how_to_apply"] == "https://forms.example.com/apply"


def test_reader_deadline_is_the_next_application_deadline():
    facts = {"deadlines": [{"kind": "행사", "at": "2026-11-12", "text": ""},
                           {"kind": "신청마감", "at": "2026-11-01", "text": ""},
                           {"kind": "신청마감", "at": "2026-11-18T23:59:59+09:00", "text": ""}]}
    assert extract.deadline_of(facts, date(2026, 11, 7))["at"] == "2026-11-18T23:59:59+09:00"
    assert extract.deadline_of(facts, date(2026, 11, 19))["at"] == "2026-11-01"  # all past: the earliest
    events = {"deadlines": [{"kind": "행사", "at": "2026-11-12", "text": ""}]}
    assert extract.deadline_of(events, date(2026, 11, 7))["kind"] == "행사"
    assert extract.next_application(events, date(2026, 11, 7)) is None


# --- one run -----------------------------------------------------------------------------------

def test_a_page_run_reads_the_page_and_stores_checked_facts_without_any_discord_job(aws, capsys):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("kw-notice", 1001, "registration-scholarship", WWW_URL))
    before = item(table, {"PK": "SRC#kw-notice", "SK": "N#1001"})
    calls, claude = [], FakeClaude()
    out = extract.collect(NOW, table, fetch=fetcher(calls), anthropic_client=claude)
    assert out == {"status": "ok", "extracted": 1} and calls == [WWW_URL] and queue(table) == []

    after = item(table, before)
    facts = after["extracted"]
    assert facts["deadlines"] == [{"kind": "신청마감", "at": "2026-11-18T23:59:59+09:00", "text": "11.18.(수) 24:00까지"}]
    assert facts["contact_office"] == "학생복지팀 (02-940-5114)" and facts["sources_used"] == ["body"]
    assert after["extract_v"] == extract.EXTRACT_V and after["model"] == "claude-opus-5-5"
    original = extract.body_text(WWW_PAGE, extract._PAGES["kw-notice"].selector)
    assert after["body_hash"] == hashlib.sha256(original.encode()).hexdigest()[:16]
    assert not any(original in str(v) for v in after.values())  # the body itself is never stored

    (request,) = claude.calls
    assert request["model"] == "claude-opus-5-5" and request["max_tokens"] == 16000
    assert request["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": extract.SCHEMA}}
    assert request["betas"] == ["server-side-fallback-2026-07-01"] and request["fallbacks"] == "default"
    assert not {"thinking", "temperature", "top_p"} & set(request)
    sent = request["messages"][0]["content"][-1]["text"]
    assert "[REDACTED_NAME]" in sent and "홍길동" not in sent and "5114" not in sent and "hong@" not in sent
    printed = capsys.readouterr().out
    assert "ExtractOk" in printed and CLAUDE_KEY not in printed and "홍길동" not in printed

    # the write is no edit: nothing the message shows, no ttl move, so no Discord job even with notify on
    assert dispatcher.jobs_for([modify(before, after)], date(2026, 11, 7), notify=True) == []

    # the same page again (a retitle that left the body as it was): no model call
    store.queue_body(table, after)
    claude.calls.clear()
    assert extract.collect(NOW + timedelta(minutes=20), table, fetch=fetcher([]), anthropic_client=claude) == {
        "status": "ok", "unchanged": 1}
    assert claude.calls == [] and queue(table) == []


def test_one_page_per_server_a_run_and_only_from_the_days_spare_requests(aws):
    table = aws["public"]
    keys(aws)
    for n in (1001, 1002):
        store.upsert(table, notice("kw-notice", n, "academic", WWW_URL.replace("1001", str(n)), when=WEEKDAY))
    store.upsert(table, notice("dept-ee", 646, "dept-ee", EE_URL, when=WEEKDAY))
    # Thursday 10:10: E1 already answered 6 list requests; with 27 list runs left its cap of 32 has none to spare
    table.put_item(Item={"PK": "ORIGIN#61.111.8.4", "SK": "STATE", "day": "2026-11-05", "day_count": 6})
    table.put_item(Item={"PK": "ORIGIN#www.kw.ac.kr", "SK": "STATE", "day": "2026-11-05", "day_count": 3})
    calls = []
    pages = {**PAGES, WWW_URL.replace("1001", "1002"): WWW_PAGE}
    out = extract.collect(WEEKDAY, table, fetch=fetcher(calls, pages), anthropic_client=FakeClaude())
    assert out == {"status": "ok", "extracted": 1, "no_spare": 1}
    assert calls == [WWW_URL]  # one www page a run: the other one waits
    assert len(queue(table)) == 2
    # 30 s later www still has spare requests (44 - 4 - (27 + 5 + 1)), but its minute is not over
    out = extract.collect(WEEKDAY + timedelta(seconds=30), table, fetch=fetcher(calls, pages), anthropic_client=FakeClaude())
    assert out["gate"] == 1 and calls == [WWW_URL]


def test_the_daily_cap_and_failing_pages(aws, capsys):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("dept-ee", 646, "dept-ee", EE_URL))
    aws["ssm"].put_parameter(Name=extract.CAP_PARAM, Value="0", Type="String")
    calls = []
    assert extract.collect(NOW, table, fetch=fetcher(calls), anthropic_client=FakeClaude()) == {
        "status": "ok", "skipped_cap": 1}
    assert calls == [] and len(queue(table)) == 1 and "ExtractSkippedCap" in capsys.readouterr().out
    aws["ssm"].put_parameter(Name=extract.CAP_PARAM, Value="5", Type="String", Overwrite=True)
    from kwcampus import params
    params.clear_cache()

    at = NOW
    for tries in (1, 2):  # a 404 is this page's own trouble: counted, the server left alone
        at += timedelta(minutes=20)
        assert extract.collect(at, table, fetch=fetcher(calls, status=("http_failure", 404)),
                               anthropic_client=FakeClaude())["failed"] == 1
        assert queue(table)[0]["tries"] == tries
    at += timedelta(minutes=20)
    extract.collect(at, table, fetch=fetcher(calls, status=("http_failure", 404)), anthropic_client=FakeClaude())
    assert queue(table) == [] and not politeness.state(table, "61.111.8.4").get("blocks")

    store.queue_body(table, notice("dept-ee", 646, "dept-ee", EE_URL))
    at += timedelta(minutes=20)
    out = extract.collect(at, table, fetch=fetcher(calls, status=("rate_limit", 429, 600)), anthropic_client=FakeClaude())
    assert out == {"status": "ok", "blocked": 1} and len(queue(table)) == 1  # the page waits
    state = politeness.state(table, "61.111.8.4")
    assert state["last_block"] == "rate_limit" and int(state["cooldown_until"]) >= at.timestamp() + 600


def test_a_refused_claude_key_falls_through_to_openai_with_the_same_facts(aws, capsys):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("kw-notice", 1001, "registration-scholarship", WWW_URL))
    send = openai_answer()
    claude = FakeClaude(error=api_error(anthropic.AuthenticationError, 401))
    out = extract.collect(NOW, table, fetch=fetcher([]), anthropic_client=claude, openai_send=send)
    assert out == {"status": "ok", "extracted": 1}
    stored = item(table, {"PK": "SRC#kw-notice", "SK": "N#1001"})
    assert stored["model"] == "gpt-5.4-mini"
    (payload, key), = send.calls
    assert key == OPENAI_KEY and payload["model"] == "gpt-5.4-mini" and payload["store"] is False
    assert payload["text"]["format"] == {"type": "json_schema", "name": "notice_facts", "strict": True,
                                         "schema": extract.SCHEMA}
    assert payload["reasoning"] == {"effort": "low"} and payload["max_output_tokens"] == 800
    printed = capsys.readouterr().out
    assert "ExtractAuthFail" in printed and CLAUDE_KEY not in printed and OPENAI_KEY not in printed

    # both providers give the same checked dict for the same answer
    original = extract.body_text(WWW_PAGE, extract._PAGES["kw-notice"].selector)
    via_claude = extract.validate(extract.ask_anthropic(stored, redact_body(original), FakeClaude())[0], stored,
                                  redact_body(original), original)
    assert stored["extracted"] == via_claude[0]


def test_without_a_claude_key_the_run_uses_openai_and_without_any_key_it_reads_nothing(aws):
    table = aws["public"]
    store.upsert(table, notice("kw-notice", 1001, "academic", WWW_URL))
    calls = []
    assert extract.collect(NOW, table, fetch=fetcher(calls)) == {"status": "ok", "auth_failed": 1}
    assert calls == [] and len(queue(table)) == 1
    keys(aws, claude=False)
    send = openai_answer()
    assert extract.collect(NOW, table, fetch=fetcher(calls), openai_send=send)["extracted"] == 1
    assert len(send.calls) == 1


def test_refusals_truncations_and_busy_apis(aws, capsys):
    table = aws["public"]
    keys(aws)
    key = {"PK": "SRC#kw-notice", "SK": "N#1001"}
    store.upsert(table, notice("kw-notice", 1001, "academic", WWW_URL))
    at = NOW
    for claude, outcome, stays in ((FakeClaude(stop="refusal"), "refused", False),
                                   (FakeClaude(stop="max_tokens"), "truncated", False),
                                   (FakeClaude(error=api_error(anthropic.RateLimitError, 429)), "retry_later", True),
                                   (FakeClaude(error=api_error(anthropic.InternalServerError, 529)), "retry_later", True),
                                   (FakeClaude(error=anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x"))),
                                    "retry_later", True)):
        store.queue_body(table, notice("kw-notice", 1001, "academic", WWW_URL))
        at += timedelta(minutes=20)
        assert extract.collect(at, table, fetch=fetcher([]), anthropic_client=claude)[outcome] == 1, outcome
        assert bool(queue(table)) is stays and "extracted" not in item(table, key)
        table.delete_item(Key={"PK": store.BODY_QUEUE, "SK": "SRC#kw-notice|N#1001"})
    printed = capsys.readouterr().out
    assert "ExtractRefused" in printed and "ExtractFail" in printed


def test_openai_http_errors_never_carry_the_key(monkeypatch):
    import urllib.error

    for code, kind in ((401, "auth"), (403, "auth"), (429, "retry_later"), (503, "retry_later"), (400, "bad_request")):
        def fail(request, timeout, context, code=code):
            raise urllib.error.HTTPError(request.full_url, code, "x", {}, None)
        monkeypatch.setattr(extract.urllib.request, "urlopen", fail)
        with pytest.raises(extract.ModelError) as caught:
            extract.openai_post({"model": "m"}, OPENAI_KEY)
        assert caught.value.kind == kind and OPENAI_KEY not in str(caught.value)
        assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_a_billing_error_counts_as_a_key_problem():
    with pytest.raises(extract.ModelError) as caught:
        extract.ask_anthropic(ITEM, "본문", FakeClaude(error=api_error(anthropic.APIStatusError, 402)))
    assert caught.value.kind == "auth"
    with pytest.raises(extract.ModelError) as caught:
        extract.ask_anthropic(ITEM, "본문", FakeClaude(error=api_error(anthropic.PermissionDeniedError, 403)))
    assert caught.value.kind == "auth"


def test_local_claude_calls_go_into_the_spend_ledger(monkeypatch, tmp_path):
    rows = tmp_path / "rows.jsonl"
    (tmp_path / "ledger.py").write_text(
        "import json\nfrom pathlib import Path\n"
        f"ROWS = Path({str(rows)!r})\n"
        "def record(pipeline, model, usage, note=''):\n"
        "    ROWS.open('a').write(json.dumps([pipeline, model, usage.input_tokens, note]) + '\\n')\n"
        "    return 0.01\n")
    monkeypatch.setenv("KW_SPEND_LEDGER", str(tmp_path / "ledger.py"))
    extract.ask_anthropic({**ITEM, "SK": "N#1"}, "본문", FakeClaude(), pipeline="notice-backfill")
    assert [json.loads(line) for line in rows.read_text().splitlines()] == [
        ["notice-backfill", "claude-opus-5-5", 1200, "N#1"]]
    monkeypatch.setenv("KW_SPEND_LEDGER", str(tmp_path / "missing.py"))  # Lambda: no ledger, no error
    extract.ask_anthropic(ITEM, "본문", FakeClaude())
    assert len(rows.read_text().splitlines()) == 1


def test_a_board_on_a_host_kw_mcp_does_not_read_yet_gets_its_body_read(aws, capsys, monkeypatch):
    # A board collected ahead of the released extension, whose reader refuses the host (kw-mcp 1.2 knows
    # 경력개발팀; the Lambda may still run an older wheel): the page run reads the body it fetched itself and
    # skips the images and files, instead of failing.
    from kw_mcp import notices

    def refuse(url):
        raise notices.NoticeError("이 주소는 아직 못 읽어요.")

    monkeypatch.setattr(notices, "check_url", refuse)
    table, url = aws["public"], "https://job.kw.ac.kr/community/notice.php?BoardMode=view&UID=12631"
    keys(aws)
    store.upsert(table, notice("dept-career", 12631, "office-career", url, title="2026 광운대학교 취업박람회 개최 안내"))
    page = '<div class="view_con"><p>취업박람회 참가 신청은 2026년 11월 18일까지 KLAS에서 받습니다.</p></div>'
    calls = []
    out = extract.collect(NOW, table, fetch=fetcher(calls, {url: page}), anthropic_client=FakeClaude())
    assert out == {"status": "ok", "extracted": 1, "assets_host": 1} and calls == [url] and queue(table) == []
    assert item(table, {"PK": "SRC#dept-career", "SK": "N#12631"})["extracted"]["sources_used"] == ["body"]
    assert '"reason": "host"' in capsys.readouterr().out


def test_the_collectors_page_runs(aws, monkeypatch):
    seen = []
    monkeypatch.setattr(extract, "collect", lambda now, table, **kw: seen.append(kw) or {"status": "idle"})
    monkeypatch.setattr(collector.boto3, "resource", lambda name: type("R", (), {"Table": lambda self, n: aws["public"]})())
    assert collector.handler({"phase": "pages"}, None) == {"status": "disabled"} and seen == []
    aws["ssm"].put_parameter(Name="/kw/collector/enabled", Value="true", Type="String")
    assert collector.handler({"phase": "pages"}, None) == {"pages": {"status": "idle"}} and len(seen) == 1


# --- what readers get ---------------------------------------------------------------------------

def stored(table, post_id, category, deadline_at, confidence="0.9"):
    from decimal import Decimal

    table.put_item(Item={"PK": "SRC#kw-notice", "SK": f"N#{post_id}", "source": "kw-notice", "post_id": str(post_id),
                         "title": f"장학 공지 {post_id}", "category_id": category, "department": "학생복지팀",
                         "posted_date": "2026-11-06", "updated_date": "2026-11-06", "pinned": False, "url": WWW_URL,
                         "extracted_at": 1762480200,
                         "extracted": {"deadlines": [{"kind": "신청마감", "at": deadline_at, "text": ""}],
                                       "target": "재학생", "eligibility": [], "how_to_apply": "KLAS",
                                       "amount": "", "contact_office": "학생복지팀 (02-940-5114)",
                                       "category_hint": "장학", "summary": "요약", "sources_used": ["body"],
                                       "confidence": Decimal(confidence)}})


def test_readers_get_the_deadline_and_can_ask_for_open_notices_only(aws, monkeypatch):
    table = aws["public"]
    monkeypatch.setattr(public_data, "_today", lambda: date(2026, 11, 10))
    stored(table, 1, "registration-scholarship", "2026-11-18T23:59:59+09:00")
    stored(table, 2, "registration-scholarship", "2026-11-05")
    table.put_item(Item={"PK": "SRC#kw-notice", "SK": "N#3", "post_id": "3", "title": "추출 전 공지", "url": WWW_URL,
                         "category_id": "registration-scholarship", "posted_date": "2026-11-06",
                         "updated_date": "2026-11-06", "department": "x", "pinned": False})
    rows = {r["post_id"]: r for r in public_data.latest(table, "장학", 10)}
    assert rows["1"]["deadline"] == {"kind": "신청마감", "at": "2026-11-18T23:59:59+09:00", "text": ""}
    assert rows["1"]["extracted"]["contact_office"] == "학생복지팀 (02-940-5114)"  # the office number survives masking
    assert rows["1"]["extracted"]["confidence"] == 0.9 and rows["1"]["extracted"]["extracted_at"].endswith("+09:00")
    assert "deadline" not in rows["3"] and "extracted" not in rows["3"]  # the old shape, unchanged
    json.dumps(rows["1"])  # plain JSON (no Decimal)
    assert [r["post_id"] for r in public_data.latest(table, "장학", 10, open_only=True)] == ["1"]
    assert [r["post_id"] for r in public_data.search(table, "장학", deadline_after=date(2026, 11, 19))] == []
    assert sorted(r["post_id"] for r in public_data.search(table, "장학", deadline_after=date(2026, 11, 1))) == ["1", "2"]


def test_the_json_api_and_mcp_tools_take_the_deadline_filters(aws, monkeypatch):
    token = "friend-token-0123456789abcdef"
    aws["ssm"].put_parameter(Name="/kw/mcp/tokens", Type="String",
                             Value=json.dumps([hashlib.sha256(token.encode()).hexdigest()]))
    monkeypatch.setattr(public_http, "_tokens", None)
    monkeypatch.setattr(public_data, "_today", lambda: date(2026, 11, 10))
    stored(aws["public"], 1, "registration-scholarship", "2026-11-18")
    stored(aws["public"], 2, "registration-scholarship", "2026-11-05")
    with TestClient(public_http.build_app()) as c:
        rows = c.get(f"/c/{token}/api/notices?category=장학&open_only=true").json()["notices"]
        assert [r["post_id"] for r in rows] == ["1"] and rows[0]["deadline"]["at"] == "2026-11-18"
        assert c.get(f"/c/{token}/api/notices?deadline_after=2026-13-01").status_code == 400
        assert len(c.get(f"/c/{token}/api/notices?category=장학").json()["notices"]) == 2
    assert [r["post_id"] for r in public_http.latest_notices("장학", open_only=True)] == ["1"]
    assert public_http.search_notices("장학", deadline_after="2026-11-01")


# --- the backfill -------------------------------------------------------------------------------

def answer_for(page_url, body, images=(), files=()):
    from kw_mcp.notices import Answer, Picture
    from kw_mcp.text import untrusted

    data = {"title": "t", "body": untrusted(body), "source_url": page_url,
            "attachments": [{"index": i, "name": name, "url": f"{page_url}&file={i}"} for i, (name, _) in enumerate(files, 1)],
            "images": [{"index": i, "url": f"{page_url}&img={i}", "alt": None} for i, _ in enumerate(images, 1)]}
    return Answer(data, [Picture(i, f"{page_url}&img={i}", "image/png", raw) for i, raw in enumerate(images, 1)])


def fake_reader(pages, log):
    """kw_mcp.notices.read_notice's shape; every request goes through the gate first, as it does there."""
    from kw_mcp.notices import Answer
    from kw_mcp.text import untrusted

    async def read(url, attachment=None, images=False, part=1, *, gate=None, transport=None, limits=None):
        body, pictures, files = pages[url]
        log.append((url, attachment, images))
        if attachment is not None:  # the page itself comes from read_notice's cache
            _, text = files[attachment - 1]
            await gate.before(f"{url}&file={attachment}")
            return Answer({"kind": "PDF", "text": untrusted(text), "total_parts": 1, "part": 1})
        await gate.before(url)
        for i, _ in enumerate(pictures if images else (), 1):
            await gate.before(f"{url}&img={i}")
        return answer_for(url, body, pictures if images else (), files)
    return read


class Spend:
    def __init__(self, left=10.0):
        self.left, self.calls = left, []

    def remaining(self, budget, pipeline):
        self.calls.append((budget, pipeline))
        return self.left


def test_backfill_picks_unread_notices_dry_runs_and_reads_images_and_files(aws):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("kw-notice", 1001, "registration-scholarship", WWW_URL, posted=date(2026, 9, 1)))
    store.upsert(table, notice("dept-ee", 646, "dept-ee", EE_URL, title="[수상] 전자공학과 4학년 홍길동"))  # may name people
    store.upsert(table, notice("kw-notice", 1002, "general", WWW_URL.replace("1001", "1002")))  # 일반: not read
    table.put_item(Item={**notice("aicon", 3514, "dept-aicon", AICON_URL), "extract_v": extract.EXTRACT_V})  # done
    rows = admin.backfill_candidates(table, None, NOW)
    assert [r["SK"] for r in rows] == ["N#646", "N#1001"]  # newest first
    assert [r["SK"] for r in admin.backfill_candidates(table, 30, NOW)] == ["N#646"]
    plan = admin.backfill_plan(table, rows, NOW)
    assert plan["candidates"] == 2 and plan["by_server"] == {"61.111.8.4": 1, "www.kw.ac.kr": 1}
    assert plan["spare_today"]["www.kw.ac.kr"] == 43  # Saturday: all of the cap but the FAQ's turn

    log, slept, clock = [], [], [NOW]
    pages = {WWW_URL: ("짧은 본문", [b"png-1", b"png-2"], [("안내문.pdf", "신청기간: 2026.11.10 ~ 11.18 문의 홍길동 010-1111-2222"),
                                                         ("합격자 명단.pdf", "명단")]),
             EE_URL: ("수상을 축하합니다. 전자공학과 홍길동 학생이 대상을 받았습니다.", [b"png-x"], [("상장.pdf", "x")])}

    async def sleep(seconds):
        slept.append(seconds)
        clock[0] += timedelta(seconds=seconds)

    claude = FakeClaude()
    spend = Spend()
    before = item(table, {"PK": "SRC#kw-notice", "SK": "N#1001"})
    counts = asyncio.run(admin.run_backfill(table, rows, 150, 25.0, spend, client=claude, now=lambda: clock[0],
                                            sleep=sleep, read=fake_reader(pages, log)))
    assert counts["extracted"] == 2 and counts["with_images"] == 1 and counts["with_attachments"] == 1
    assert (EE_URL, None, False) in log and not any(a for u, a, _ in log if u == EE_URL)  # body only
    assert (WWW_URL, None, True) in log and (WWW_URL, 1, False) in log and (WWW_URL, 2, False) not in log
    www = item(table, {"PK": "SRC#kw-notice", "SK": "N#1001"})
    assert www["extracted"]["sources_used"] == ["body", "images", "attachments"]
    # a backfill write is no notice edit: no Discord job, notifications on or off
    assert dispatcher.jobs_for([modify(before, www)], date(2026, 11, 7), notify=True) == []
    assert {k for k in www if k not in before} == {"extracted", "extract_v", "extracted_at", "model", "body_hash"}
    request = next(c for c in claude.calls if len(c["messages"][0]["content"]) > 1)
    blocks = request["messages"][0]["content"]
    assert [b["type"] for b in blocks] == ["image", "image", "text"]
    assert "[첨부: 안내문.pdf]" in blocks[-1]["text"] and "010-1111-2222" not in blocks[-1]["text"]
    assert "홍길동" not in blocks[-1]["text"]
    assert spend.calls and all(c == (25.0, "notice-backfill") for c in spend.calls)
    # requests to one server went out a minute apart through the collectors' gate
    assert slept and politeness.state(table, "www.kw.ac.kr")["day_count"] == 4  # page, 2 images, 1 file
    # stop and run again: nothing left to pick
    assert admin.backfill_candidates(table, None, NOW) == []


def test_backfill_stops_at_the_budget_and_notes_pages_with_nothing_to_read(aws):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("kw-notice", 1001, "academic", WWW_URL))
    store.upsert(table, notice("dept-ee", 646, "dept-ee", EE_URL))
    rows = admin.backfill_candidates(table, None, NOW)
    pages = {WWW_URL: ("", [], []), EE_URL: ("졸업작품 최종보고서를 11월 20일까지 학과 사무실로 제출하세요.", [], [])}
    claude = FakeClaude()

    async def sleep(seconds):
        return None

    counts = asyncio.run(admin.run_backfill(table, rows, 150, 25.0, Spend(left=0.2), client=claude,
                                            now=lambda: NOW, sleep=sleep, read=fake_reader(pages, [])))
    assert counts["budget_used"] == 1 and counts["nothing_to_read"] == 1 and claude.calls == []
    empty = item(table, {"PK": "SRC#kw-notice", "SK": "N#1001"})
    assert empty["extract_v"] == extract.EXTRACT_V and "extracted" not in empty  # not picked again, not shown
    assert [r["SK"] for r in admin.backfill_candidates(table, None, NOW)] == ["N#646"]


def test_backfill_gate_waits_its_turn_and_refuses_what_the_collectors_would_not_ask(aws):
    from kw_mcp.notices import Refused

    table = aws["public"]
    clock, slept = [NOW], []

    async def sleep(seconds):
        slept.append(seconds)
        clock[0] += timedelta(seconds=seconds)

    gate = admin.PoliteGate(table, lambda: clock[0], sleep, refused=Refused)
    asyncio.run(gate.before(WWW_URL))
    asyncio.run(gate.before(WWW_URL + "&img=1"))  # the same server: waits out the minute
    assert slept and 60 <= sum(slept) <= 62
    with pytest.raises(Refused):
        asyncio.run(gate.before("https://static.wixstatic.com/x.png"))
    table.put_item(Item={"PK": "ORIGIN#61.111.8.4", "SK": "STATE", "day": "2026-11-07", "day_count": 32})
    with pytest.raises(Refused):
        asyncio.run(gate.before(EE_URL))


def test_backfill_extra_today_only_when_no_list_run_is_left(aws):
    """--extra-today lifts our own daily cap, but only while the collectors need nothing more from the
    server today: they check the plain cap, so a weekday-morning extra would cost them their runs."""
    from kw_mcp.notices import Refused

    table = aws["public"]

    async def sleep(seconds):
        return None

    saturday = datetime(2026, 11, 7, 10, tzinfo=UTC)  # 19:00 KST, a weekend: no list runs
    monday = datetime(2026, 11, 9, 1, tzinfo=UTC)  # 10:00 KST, list runs still to come
    table.put_item(Item={"PK": "ORIGIN#61.111.8.4", "SK": "STATE", "day": "2026-11-07", "day_count": 32})
    asyncio.run(admin.PoliteGate(table, lambda: saturday, sleep, refused=Refused, extra_today=5).before(EE_URL))
    item = table.get_item(Key={"PK": "ORIGIN#61.111.8.4", "SK": "STATE"})["Item"]
    assert int(item["day_count"]) == 33 and int(item["next_at"]) == int((saturday + timedelta(seconds=60)).timestamp())
    with pytest.raises(Refused):  # the extra is spent: the plain cap 32 + 5 is reached
        table.put_item(Item={**item, "day_count": 37, "next_at": 0})
        asyncio.run(admin.PoliteGate(table, lambda: saturday, sleep, refused=Refused, extra_today=5).before(EE_URL))
    table.put_item(Item={"PK": "ORIGIN#61.111.8.4", "SK": "STATE", "day": "2026-11-09", "day_count": 32})
    with pytest.raises(Refused):
        asyncio.run(admin.PoliteGate(table, lambda: monday, sleep, refused=Refused, extra_today=5).before(EE_URL))


def test_backfill_transport_tells_the_gate_what_servers_answer(aws):
    table = aws["public"]
    answers = iter([httpx.Response(429, headers={"Retry-After": "120"}), httpx.Response(200, text="ok")])
    inner = httpx.MockTransport(lambda request: next(answers))

    async def go():
        async with httpx.AsyncClient(transport=admin.WatchedTransport(table, lambda: NOW, inner)) as c:
            await c.get(WWW_URL)
            assert politeness.state(table, "www.kw.ac.kr")["last_block"] == "rate_limit"
            await c.get(WWW_URL)
    asyncio.run(go())
    state = politeness.state(table, "www.kw.ac.kr")
    assert state["blocks"] == 0 and int(state["cooldown_until"]) >= NOW.timestamp() + 120


def test_backfill_gap_spaces_requests_closer_than_the_collectors(aws):
    """--gap lets an operator's catch-up run wait less than the collectors' minute; the gate still waits it."""
    from kw_mcp.notices import Refused

    table = aws["public"]
    clock, slept = [datetime(2026, 11, 7, 10, tzinfo=UTC)], []

    async def sleep(seconds):
        slept.append(seconds)
        clock[0] += timedelta(seconds=seconds)

    gate = admin.PoliteGate(table, lambda: clock[0], sleep, refused=Refused, extra_today=50, gap_s=3)
    asyncio.run(gate.before(EE_URL))
    asyncio.run(gate.before(EE_URL))
    assert slept and 3 <= sum(slept) <= 5
    assert admin.PoliteGate(table, lambda: clock[0], sleep, gap_s=0.1).interval == timedelta(seconds=1)


# --- page runs: images and attachments (notice_assets) -------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + (800).to_bytes(4, "big") + (1131).to_bytes(4, "big") + b"\x08\x06" + b"\0" * 64
POSTER_PAGE = ('<html><body><div class="board-view-box"><ul><li class="title">제목</li>'
               '<li class="contents"><p><img src="/upload/poster.png"></p></li></ul></div></body></html>')
EE_SHORT = '<div class="view_con"><p>첨부 파일을 보세요.</p></div>'


def page_reader(log, pictures=(), files=()):
    """kw_mcp.notices.read_notice's shape for a page run: the page through the gate (in hand: nothing
    goes out), every image and file through the gate too."""
    from kw_mcp.notices import Answer, Picture, check_url
    from kw_mcp.text import untrusted

    async def read(url, attachment=None, images=False, part=1, *, gate=None, transport=None, limits=None):
        log.append((attachment, images))
        if attachment is not None:
            await gate.before(f"{url}&file={attachment}")
            return Answer({"kind": "PDF", "text": untrusted(files[attachment - 1][1]), "total_parts": 1, "part": 1})
        await gate.before(check_url(url))
        shown = []
        for i, raw in enumerate(pictures if images else (), 1):
            await gate.before(f"{url}&img={i}")
            shown.append(Picture(i, f"{url}&img={i}", "image/png", raw))
        return Answer({"body": untrusted("x"),
                       "images": [{"index": i, "url": f"{url}&img={i}"} for i in range(1, len(pictures) + 1)],
                       "attachments": [{"index": i, "name": n, "url": f"{url}&file={i}"}
                                       for i, (n, _) in enumerate(files, 1)]}, shown)
    return read


def ticking():
    clock, slept = [NOW], []

    async def sleep(seconds):
        slept.append(seconds)
        clock[0] += timedelta(seconds=seconds)
    return clock, slept, sleep


def test_a_poster_only_notice_gets_its_facts_from_the_poster_through_openai(aws, monkeypatch):
    """kw_mcp.notices itself reads the page the run already has (no second page request) and the poster
    3 s later; both count for www's cap, and www gets its minute back afterwards."""
    monkeypatch.setattr(extract, "PROVIDER", "openai")
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("kw-notice", 1001, "registration-scholarship", WWW_URL))
    asked = []

    def network(request):
        asked.append(str(request.url))
        return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)

    clock, slept, sleep = ticking()
    send, calls = openai_answer(), []
    out = extract.collect(NOW, table, fetch=fetcher(calls, {WWW_URL: POSTER_PAGE}), clock=lambda: clock[0],
                          openai_send=send, read=REAL_READ, inner_transport=lambda: httpx.MockTransport(network),
                          sleep=sleep)
    assert out == {"status": "ok", "extracted": 1}
    assert calls == [WWW_URL] and asked == ["https://www.kw.ac.kr/upload/poster.png"]
    stored = item(table, {"PK": "SRC#kw-notice", "SK": "N#1001"})
    assert stored["extracted"]["sources_used"] == ["images"] and stored["model"] == "gpt-5.4-mini"
    assert stored["extracted"]["deadlines"][0]["at"] == "2026-11-18T23:59:59+09:00"
    (payload, _), = send.calls
    image, text = payload["input"][0]["content"]
    assert image == {"type": "input_image", "detail": "auto",
                     "image_url": "data:image/png;base64," + base64.b64encode(PNG).decode()}
    assert text["type"] == "input_text" and "본문:" in text["text"]
    state = politeness.state(table, "www.kw.ac.kr")
    assert int(state["day_count"]) == 2 and 3 <= sum(slept) <= 5  # page, poster ASSET_GAP_S later
    assert int(state["next_at"]) >= int(clock[0].timestamp()) + 60  # the list runs' minute is back


def test_an_attachment_only_deadline_is_read_and_roster_files_are_not(aws, capsys):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("dept-ee", 646, "dept-ee", EE_URL))
    log, claude = [], FakeClaude()
    clock, _, sleep = ticking()
    files = [("모집 안내.pdf", "접수 마감: 2026년 11월 18일 24:00까지, 문의 홍길동 010-1111-2222"), ("합격자 명단.pdf", "명단")]
    out = extract.collect(NOW, table, fetch=fetcher([], {EE_URL: EE_SHORT}), clock=lambda: clock[0],
                          anthropic_client=claude, read=page_reader(log, files=files), sleep=sleep)
    assert out == {"status": "ok", "extracted": 1}
    assert (1, False) in log and (2, False) not in log  # the roster-like file is never asked for
    stored = item(table, {"PK": "SRC#dept-ee", "SK": "N#646"})
    assert stored["extracted"]["sources_used"] == ["body", "attachments"]
    sent = claude.calls[0]["messages"][0]["content"][-1]["text"]
    assert "[첨부: 모집 안내.pdf]" in sent and "11월 18일" in sent and "010-1111-2222" not in sent and "홍길동" not in sent
    assert int(politeness.state(table, "61.111.8.4")["day_count"]) == 2  # page and file, both counted


def test_a_title_that_may_name_people_sends_no_images_or_files(aws):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("dept-ee", 646, "dept-ee", EE_URL, title="[수상] 전자공학과 4학년 홍길동"))
    log, claude = [], FakeClaude()
    out = extract.collect(NOW, table, fetch=fetcher([]), anthropic_client=claude,
                          read=page_reader(log, pictures=[PNG], files=[("안내.pdf", "x")]))
    assert out == {"status": "ok", "extracted": 1} and log == []
    assert [b["type"] for b in claude.calls[0]["messages"][0]["content"]] == ["text"]
    state = politeness.state(table, "61.111.8.4")
    assert int(state["day_count"]) == 1 and int(state["next_at"]) == int(NOW.timestamp()) + 60  # the usual minute


def test_without_spare_requests_for_every_image_and_file_the_body_goes_alone(aws, capsys):
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("kw-notice", 1001, "academic", WWW_URL, when=WEEKDAY))
    # Thursday 10:10: 44 - 5 - (27 + 5 + 1) = 6 to spare; the page takes one, the notice wants 6 more
    table.put_item(Item={"PK": "ORIGIN#www.kw.ac.kr", "SK": "STATE", "day": "2026-11-05", "day_count": 5})
    log, claude = [], FakeClaude()
    read = page_reader(log, pictures=[PNG] * 5, files=[("안내.pdf", "x"), ("양식.hwp", "y")])
    out = extract.collect(WEEKDAY, table, fetch=fetcher([]), anthropic_client=claude, read=read)
    assert out == {"status": "ok", "extracted": 1, "assets_no_spare": 1}
    assert log == [(None, False)]  # the page's list, from memory; nothing requested
    assert item(table, {"PK": "SRC#kw-notice", "SK": "N#1001"})["extracted"]["sources_used"] == ["body"]
    assert int(politeness.state(table, "www.kw.ac.kr")["day_count"]) == 6
    assert "ExtractAssetsSkipped" in capsys.readouterr().out


def test_asset_requests_stop_at_the_notice_budget_and_the_daily_spare(aws):
    from kw_mcp.notices import Refused

    from kwcampus import notice_assets

    table = aws["public"]
    clock, slept, sleep = ticking()
    gate = notice_assets.PoliteGate(table, lambda: clock[0], sleep, refused=Refused, gap_s=politeness.ASSET_GAP_S,
                                    budget=2)
    for i in (1, 2):
        asyncio.run(gate.before(f"{EE_URL}&img={i}"))
    with pytest.raises(Refused):
        asyncio.run(gate.before(f"{EE_URL}&img=3"))
    assert 3 <= sum(slept) <= 5 and gate.origins == {"61.111.8.4"}
    gate.rest()
    assert int(politeness.state(table, "61.111.8.4")["next_at"]) == int(clock[0].timestamp()) + 60
    table.put_item(Item={"PK": "ORIGIN#61.111.8.4", "SK": "STATE", "day": "2026-11-07", "day_count": 32})
    with pytest.raises(Refused):  # no spare left: never borrowed from the list runs
        asyncio.run(notice_assets.PoliteGate(table, lambda: clock[0], sleep, refused=Refused).before(EE_URL))


def test_a_hosts_crawl_delay_stretches_the_asset_gap(aws, monkeypatch):
    """job.kw.ac.kr's robots.txt asks for Crawl-delay: 10. A notice's images and files there wait 10 s, not
    ASSET_GAP_S, both after the page request (extract.collect) and between each other (PoliteGate)."""
    from kw_mcp.notices import Refused

    from kwcampus import notice_assets

    job = "https://job.kw.ac.kr/community/notice.php?BoardMode=view&UID=12631"
    assert politeness.crawl_delay(job) == 10 and politeness.crawl_delay(EE_URL) == 0
    assert politeness.crawl_delay("https://unchecked.example/") == 0
    table = aws["public"]
    clock, slept, sleep = ticking()
    gate = notice_assets.PoliteGate(table, lambda: clock[0], sleep, refused=Refused, gap_s=politeness.ASSET_GAP_S)
    for i in (1, 2, 3):
        asyncio.run(gate.before(f"https://job.kw.ac.kr/upload/poster{i}.png"))
    assert 2 * 10 <= sum(slept) <= 2 * 12 and len(slept) == 2  # 10 s each, never 3
    assert int(politeness.state(table, "61.111.8.4")["next_at"]) - int(clock[0].timestamp()) == 10
    gate.rest()
    # the same server's other hosts keep the 3 s gap
    clock2, slept2, sleep2 = ticking()
    clock2[0] = clock[0] + timedelta(hours=1)
    other = notice_assets.PoliteGate(table, lambda: clock2[0], sleep2, refused=Refused, gap_s=politeness.ASSET_GAP_S)
    for i in (1, 2):
        asyncio.run(other.before(f"{EE_URL}&img={i}"))
    assert 3 <= sum(slept2) <= 5

    # the page request of a page run sets the same longer gap for the first image
    asked = []
    real = politeness.acquire
    monkeypatch.setattr(politeness, "acquire", lambda *a, **kw: asked.append(kw.get("interval")) or real(*a, **kw))
    keys(aws)
    store.upsert(table, notice("dept-career", 12631, "office-career", job, title="2026 광운대학교 취업박람회 개최 안내"))
    page = '<div class="view_con"><p>취업박람회 참가 신청은 2026년 11월 18일까지 KLAS에서 받습니다.</p></div>'
    extract.collect(NOW + timedelta(hours=2), table, fetch=fetcher([], {job: page}), anthropic_client=FakeClaude())
    assert asked == [timedelta(seconds=10)]


def test_openai_takes_images_and_a_used_up_quota_counts_as_a_key_problem(aws, monkeypatch):
    import io
    import urllib.error

    keys(aws)
    send = openai_answer()
    extract.ask_openai(ITEM, "본문", send, (("image/jpeg", b"jpg"),))
    content = send.calls[0][0]["input"][0]["content"]
    assert content[0] == {"type": "input_image", "detail": "auto", "image_url": "data:image/jpeg;base64,anBn"}
    assert content[1]["type"] == "input_text"
    extract.ask_openai(ITEM, "본문", send)
    assert isinstance(send.calls[1][0]["input"], str)  # text only: the plain string, as before

    for body, kind in ((b'{"error": {"code": "insufficient_quota", "type": "insufficient_quota"}}', "auth"),
                       (b'{"error": {"code": "rate_limit_exceeded"}}', "retry_later")):
        def fail(request, timeout, context, body=body):
            raise urllib.error.HTTPError(request.full_url, 429, "x", {}, io.BytesIO(body))
        monkeypatch.setattr(extract.urllib.request, "urlopen", fail)
        with pytest.raises(extract.ModelError) as caught:
            extract.openai_post({"model": "m"}, OPENAI_KEY)
        assert caught.value.kind == kind


def test_with_openai_chosen_a_failing_key_falls_back_to_claude_only_if_its_key_is_there(aws, monkeypatch, capsys):
    monkeypatch.setattr(extract, "PROVIDER", "openai")
    table = aws["public"]
    keys(aws)
    store.upsert(table, notice("kw-notice", 1001, "academic", WWW_URL))

    def refused(payload, key):
        raise extract.ModelError("auth", 429)

    claude = FakeClaude()
    out = extract.collect(NOW, table, fetch=fetcher([]), anthropic_client=claude, openai_send=refused)
    assert out == {"status": "ok", "extracted": 1} and len(claude.calls) == 1
    assert item(table, {"PK": "SRC#kw-notice", "SK": "N#1001"})["model"] == "claude-opus-5-5"

    aws["ssm"].delete_parameter(Name=extract.KEY_PARAMS["anthropic"])
    extract._keys.clear()
    store.upsert(table, notice("kw-notice", 1002, "academic", WWW_URL.replace("1001", "1002")))
    capsys.readouterr()
    out = extract.collect(NOW + timedelta(minutes=20), table, fetch=fetcher([], {WWW_URL.replace("1001", "1002"): WWW_PAGE}),
                          openai_send=refused)
    assert out == {"status": "ok", "auth_failed": 1} and len(queue(table)) == 1  # waits for a later run
    assert capsys.readouterr().out.count('"ExtractAuthFail": 1') == 1  # OpenAI's; no Claude key is no failure


def test_the_privacy_page_names_openai_for_the_summaries():
    from kwcampus.bridge import install

    page = install.privacy_page()
    assert "OpenAI의 API로 만들고" in page and "Anthropic의 Claude API" in page
    assert "본문만 보내고 이미지와 첨부는 보내지 않습니다" in page and f"{politeness.ASSET_GAP_S}초" in page
