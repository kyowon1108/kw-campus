"""Review 3 fixes for the collectors and Discord.

S46 names redaction v2 let through, S47 a second pass changes nothing, S48 "02)940-5114" and Unicode
dashes (and the v3 bump edits stored notices quietly), S49 a takedown stays down, S50 delivery
records follow their notice, S51 a source that keeps failing on its own needs a human, S53 긴급
look-alikes, S55 robots.txt records that get old and robots_skip in www and campus, S60 429s waited out.
Second pass: no ordinary word masked after a bare 대상 or 심사/졸업생/인터뷰/우승, prizes like 우수논문상,
names behind a department or year, a linear mask comparison in the dispatcher, and "keep" jobs.
"""

import importlib.util
import io
import json
import urllib.error
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from test_delivery import (
    DAY,
    EDIT,
    JOB,
    NOTICE,
    ROLES,
    FakeDiscord,
    retitle,
    run,
    snowflake,
    stream,
)
from test_redaction import CONTACTS, NAMES, UNCHANGED

from kwcampus import (
    campus,
    discord,
    discordapi,
    dispatcher,
    politeness,
    routing,
    wwwboards,
)
from kwcampus.config import DELIVERY_TTL, KST, NOTICE_TTL
from kwcampus.fetch import FetchResult
from kwcampus.kwnotice.redaction import (
    NAME,
    REDACTION_LEVEL,
    REDACTION_VERSION,
    redact_text,
)

N = NAME
FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 5, 3, 0, tzinfo=UTC)
TOKEN = "MTIzNDU2Nzg5MDEyMzQ1Njc4.GabcDE.fake_token_value_for_tests_only_xx"  # not a real token
DKEY = {"PK": f"{NOTICE['PK']}|{NOTICE['SK']}", "SK": "CH#academic"}

_spec = importlib.util.spec_from_file_location("collector_admin",
                                               Path(__file__).parents[1] / "scripts" / "collector_admin.py")
admin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(admin)


def metric_names(capsys) -> list[str]:
    out = []
    for line in capsys.readouterr().out.splitlines():
        doc = json.loads(line)
        if "_aws" in doc:
            out += [m["Name"] for m in doc["_aws"]["CloudWatchMetrics"][0]["Metrics"] if doc[m["Name"]]]
    return out


def log_events(text: str) -> list[dict]:
    return [doc for doc in map(json.loads, text.splitlines()) if "event" in doc]


# --- S46: names v2 let through ------------------------------------------------------------------

NEW_NAMES = [
    # a name with its affiliation in brackets before the role (no gap after the name)
    ("[축하] 홍길동(전자공학과) 학생 우수논문상 수상", f"[축하] {N}(전자공학과) 학생 우수논문상 수상"),
    ("김철수(소프트웨어학부)학생 창업 성공기", f"{N}(소프트웨어학부)학생 창업 성공기"),
    ("이영희[로봇학부] 교수 연구팀 논문 게재", f"{N}[로봇학부] 교수 연구팀 논문 게재"),
    ("홍길동 (전자공학과 4학년) 학생 장학금 수혜", f"{N} (전자공학과 4학년) 학생 장학금 수혜"),
    ("김민준(전자공학과) 외 2명 논문상 수상", f"{N}(전자공학과) 외 2명 논문상 수상"),
    ("박영희(경영학부) 대상 수상", f"{N}(경영학부) 대상 수상"),
    # one name right after a roster word
    ("학위논문 공개발표 - 홍길동", f"학위논문 공개발표 - {N}"),
    ("2026 석사학위논문 공개발표 안내 - 김철수", f"2026 석사학위논문 공개발표 안내 - {N}"),
    ("최종합격 홍길동", f"최종합격 {N}"),
    ("2026 후기 박사학위 논문심사 결과: 이영희", f"2026 후기 박사학위 논문심사 결과: {N}"),
    ("합격자: 김민준", f"합격자: {N}"),
    ("졸업생 인터뷰 홍길동", f"졸업생 인터뷰 {N}"),
    ("[인터뷰] 김철수, 창업 이야기", f"[인터뷰] {N}, 창업 이야기"),
    ("[수상] 홍길동 IEEE 학술대회 최우수논문상", f"[수상] {N} IEEE 학술대회 최우수논문상"),
    # a name before 외 without a count
    ("2026-2 장학생 선발 결과 (김민준 외)", f"2026-2 장학생 선발 결과 ({N} 외)"),
    ("우수논문상 수상 (김산 외)", f"우수논문상 수상 ({N} 외)"),
    ("홍길동 외 연구팀 논문 게재", f"{N} 외 연구팀 논문 게재"),
    # award lists, and a name before a prize or a pass
    ("2026 캡스톤디자인 경진대회 대상 홍길동, 금상 김철수 이영희",
     f"2026 캡스톤디자인 경진대회 대상 {N}, 금상 {N} {N}"),
    ("최우수상: 김민준 / 우수상: 이서연", f"최우수상: {N} / 우수상: {N}"),
    ("총장상 홍길동", f"총장상 {N}"),
    ("전국 대학생 해커톤 우승 김철수 팀", f"전국 대학생 해커톤 우승 {N} 팀"),
    ("홍길동 최우수상 수상", f"{N} 최우수상 수상"),
    ("2026 변리사 시험 김철수 합격", f"2026 변리사 시험 {N} 합격"),
    ("홍길동 대상 수상", f"{N} 대상 수상"),
    ("IEEE 학술상 수상자 이영희, 김산", f"IEEE 학술상 수상자 {N}, {N}"),
    # prizes that end like a common word (논문상: 문상, 봉사상: 사상, 신인상: 인상, 교육감상: 감상)
    ("2026 학술대회 우수논문상 홍길동, 김철수", f"2026 학술대회 우수논문상 {N}, {N}"),
    ("춘계학술대회 최우수논문상 김민준 이서연", f"춘계학술대회 최우수논문상 {N} {N}"),
    ("봉사상 홍길동", f"봉사상 {N}"),
    ("신인상 이영희", f"신인상 {N}"),
    ("교육감상 김철수", f"교육감상 {N}"),
    ("홍길동(전자공학과) 우수논문상 수상", f"{N}(전자공학과) 우수논문상 수상"),
    # the person's department or year between the roster word and the name
    ("[수상] 전자공학과 홍길동", f"[수상] 전자공학과 {N}"),
    ("[수상] 전자공학과 4학년 홍길동", f"[수상] 전자공학과 4학년 {N}"),
    ("[수상] 소프트웨어학부 박사과정 김민준", f"[수상] 소프트웨어학부 박사과정 {N}"),
    ("2026 우수상 수상 축하 전자공학과 졸업 홍길동", f"2026 우수상 수상 축하 전자공학과 졸업 {N}"),
    # a bare 대상 that is the grand prize: after a contest, or next to another prize
    ("공모전 대상 홍길동", f"공모전 대상 {N}"),
    ("경진대회 대상 김산, 이훈", f"경진대회 대상 {N}, {N}"),
    ("대상 홍길동, 금상 김철수", f"대상 {N}, 금상 {N}"),
    ("해커톤 우승자 김산, 이훈", f"해커톤 우승자 {N}, {N}"),  # 우승자 is a roster like 수상자
    # after a bare 심사/졸업생/인터뷰/우승: full names right after it or in brackets
    ("졸업생 인터뷰 (홍길동)", f"졸업생 인터뷰 ({N})"),
    ("졸업생 인터뷰 (홍길동, 김철수)", f"졸업생 인터뷰 ({N}, {N})"),
]

NEW_CONTACTS = [
    ("문의 02)940-5114", "문의 [REDACTED_PHONE]"),  # v1 masked it, v2 did not
    ("문의 02) 940-5114", "문의 [REDACTED_PHONE]"),
    ("문의 02–940–5114", "문의 [REDACTED_PHONE]"),  # en dashes
    ("문의 010‒1234‒5678", "문의 [REDACTED_PHONE]"),  # figure dashes
    ("연락처 010－1234－5678", "연락처 [REDACTED_PHONE]"),  # fullwidth hyphen-minus
    ("문의 010)1234-5678", "문의 [REDACTED_PHONE]"),
]

# Look-alikes of the new rules: prizes that are no prize (이상, 영상, 옥상), 대상 meaning "for",
# 외 after an ordinary word, and words right after a roster word that are no name.
NEW_UNCHANGED = [
    "재학생 대상 진로 특강 안내", "예비군 대상 교육 안내", "임직원 대상 설문조사", "합격자 등록 안내",
    "동영상 강의 수강 안내", "평점 3.0 이상 장학생 선발 안내", "학위논문 심사 일정 안내",
    "2026-2 학위논문 제출 안내(최종본)", "논문 심사 결과 공지 (공개용)", "[인터뷰] 졸업생이 들려주는 취업 이야기",
    "수상작 전시 안내", "공모전 수상작 발표", "2026 캡스톤디자인 경진대회 시상식 개최", "그 외 문의 사항 안내",
    "도서관 외 시설 이용 안내", "정원 외 모집 안내", "수상 소감 발표회", "최우수상 시상 및 수여식",
    "공무원 합격 수기 공모", "국내외 수상 실적 제출 안내", "본교 연구진 우수상 수상", "임직원(교원) 대상 교육",
    "신입생(편입생 포함) 대상 오리엔테이션", "졸업생 대상 설문조사 안내", "학위논문 심사본 제출 안내",
    "[대상] 전체 재학생", "2026 홍보영상 공모전 대상 발표", "동상 제막식 안내", "옥상 출입 통제 안내",
    "기상 악화로 인한 휴강 안내", "폐쇄회로(CCTV) 설치 안내",
    "[취업] [비알코리아x파리크라상x삼립] 2026년 하반기 대졸 신입 공채", "장학생 선정 결과 안내문 확인",
    "수상자 기념품 수령 안내", "2026 경진대회 대상 및 금상 시상", "학생 대상 안내문 배부", "주말 외 평일 운영",
    "지역주민 대상 무료 강좌",
    # a bare 대상 meaning "for" is no roster word
    "2026학년도 재학생 대상 성희롱·성폭력 예방교육 안내", "재학생 대상 이벤트 안내", "신입생 대상 성교육 실시",
    "재학생 대상 진로 설계 특강", "2026 공모전 대상 확대 안내", "공모전 대상 및 응모 방법 안내",
    # after a bare 심사/졸업생/인터뷰/우승, and words right after a roster word that are no name
    "졸업생 진로 현황 조사", "합격 축하 이벤트 안내", "심사 전문가 모집", "심사 기준표 안내", "졸업생 소식지 발간",
    "합격 예정자 안내", "졸업생 추천서 발급 안내", "합격자 서약서 제출 안내", "합격자 신분증 지참 안내",
    "합격자 예치금 납부 안내", "수상 연구비 지원 안내", "심사 전형료 환불 안내", "합격자 이력서 제출",
    "졸업생 성공사례 공유회", "수상 소감 영상 공개", "[수상] 전자공학과 학생팀 대상",
    "[수상] 전자공학과 로봇 동아리 우승", "합격자 전자공학과 오리엔테이션 안내",
    # words that end in 상 and are no prize: 항상, 임상, 의상, 통상, and "in terms of" (시스템상 ...)
    "항상 열려 있는 학생상담센터", "임상 실습 오리엔테이션", "의상 대여 서비스 안내", "국제통상 특강 안내",
    "시스템상 오류로 인한 수강신청 연기 안내", "전산상 문제로 성적 공개 일정 변경", "절차상 하자로 재공고",
    "인터넷상 개인정보 노출 주의 안내", "계약상 문제로 공사 일정 변경", "안전상 이유로 출입 통제",
    "역량향상 프로그램 운영 안내", "등록금인상 관련 설명회", "음악감상 동아리 모집",
]


@pytest.mark.parametrize(("title", "expected"), NEW_NAMES + NEW_CONTACTS)
def test_names_and_numbers_v2_let_through_are_masked(title, expected):
    assert redact_text(title) == expected


@pytest.mark.parametrize("title", NEW_UNCHANGED)
def test_look_alikes_of_the_new_rules_stay_as_they_are(title):
    assert redact_text(title) == title


def test_the_level_went_up_with_the_rules():
    assert (REDACTION_VERSION, REDACTION_LEVEL) == ("v3", 3)


# --- S47: a second pass changes nothing ---------------------------------------------------------

ALL_TITLES = [t for t, _ in NAMES + CONTACTS + NEW_NAMES + NEW_CONTACTS] + UNCHANGED + NEW_UNCHANGED


@pytest.mark.parametrize("title", ALL_TITLES)
def test_a_second_pass_changes_nothing(title):
    once = redact_text(title)
    assert redact_text(once) == once


def test_masks_are_not_masked_again():
    for masked in ("학번 [REDACTED_IDENTIFIER] 확인", "학번: [REDACTED_IDENTIFIER]", "학번([REDACTED_IDENTIFIER])",
                   "계좌 [REDACTED_ACCOUNT]", "수험번호 [REDACTED_IDENTIFIER]"):
        assert redact_text(masked) == masked
    # what public_data serves for a title stored under v1 or v2 (it masks again on the way out)
    stored = redact_text("학번 2021123456 홍길동 학생")
    assert stored == f"학번 [REDACTED_IDENTIFIER] {N} 학생" and redact_text(stored) == stored


# --- S48 and the bump: stored notices are edited quietly ----------------------------------------

# (the title on the site, what an earlier version stored) where that differs from the title itself
OLDER = [
    ("문의 02)940-5114", "문의 [REDACTED_PHONE]"),  # v1
    ("문의 02) 940-5114", "문의 [REDACTED_PHONE]"),  # v1
    ("2026-2 학위논문 제출 안내(최종본)", f"2026-2 학위논문 제출 안내({N})"),  # v2 masked a word that is no name
    ("논문 심사 결과 공지 (공개용)", f"논문 심사 결과 공지 ({N})"),  # v2
    ("합격자 명단: 2021123456 홍길동", "합격자 명단: [REDACTED_ACCOUNT] 홍길동"),  # v1
    ("[축하] 전자공학과 홍길동 학생 대상 수상 / 학번 2021123456",
     "[축하] 전자공학과 홍길동 학생 대상 수상 / 학번 [REDACTED_IDENTIFIER]"),  # v1
    ("IEEE 학술상 수상자 이영희, 김산", f"IEEE 학술상 수상자 {N}, {N}"),  # v2
]
STORED = [(raw, raw) for raw in ALL_TITLES] + OLDER  # (raw, stored): older rules may have masked nothing


def _rerun(stored: str, raw: str, old_norm: int | None, updated: str = "2026-10-15") -> list[dict]:
    old = {**NOTICE, "title": stored, "updated_date": "2026-10-15"}
    if old_norm is not None:
        old["norm_v"] = old_norm
    new = {**NOTICE, "title": redact_text(raw), "updated_date": updated, "norm_v": REDACTION_LEVEL}
    return dispatcher.jobs_for([stream("MODIFY", new, old=old)], date(2026, 11, 6))


@pytest.mark.parametrize(("raw", "stored"), STORED)
@pytest.mark.parametrize("old_norm", [None, 1, 2])
def test_the_v3_bump_edits_stored_notices_quietly(raw, stored, old_norm):
    jobs = _rerun(stored, raw, old_norm)
    assert all(j["action"] == "edit" and j.get("quiet") for j in jobs), (stored, redact_text(raw))


def test_school_edits_are_still_loud_after_the_bump():
    # the site's 수정일 moved
    assert not _rerun("문의 [REDACTED_PHONE]", "문의 02)940-5114", 2, updated="2026-11-06")[0].get("quiet")
    # a new word outside the masks
    assert not _rerun(f"{N} 학생 수상", "홍길동 학생 수상 취소", 2)[0].get("quiet")
    assert not _rerun("KLAS 보안 강화 안내", "KLAS 보안 강화 안내 (기간 연장)", 2)[0].get("quiet")
    # a title that is mostly masks says too little: a real edit
    assert not _rerun(N, "완전히 다른 제목", 2)[0].get("quiet")
    # the same rules: norm_v did not change, so any title change is the school's
    assert not _rerun("2026-2 학위논문 제출 안내", "2026-2 학위논문 제출 안내(최종본)", REDACTION_LEVEL)[0].get("quiet")


def test_only_what_a_name_mask_covered_may_come_back():
    explained = dispatcher._explained_by_masks
    # a mask undone: one 2-4 syllable word where it stood, the rest the same
    assert explained(f"2026-2 학위논문 제출 안내({N})", "2026-2 학위논문 제출 안내(최종본)")
    assert explained(f"논문 심사 결과 공지 ({N}) 및 일정", "논문 심사 결과 공지 (공개용) 및 일정")
    assert explained(f"공개발표 안내 {N}님 외", "공개발표 안내 담당자님 외")
    # the school added words next to a mask: a real edit
    assert not explained(f"2026 장학생 명단: {N}, {N}", f"2026 장학생 명단: {N}, {N} (추가)")
    assert not explained(f"2026 장학생 명단: {N}, {N}", f"2026 장학생 명단: {N}, {N}, {N}")
    assert not explained(f"2026-2 학위논문 제출 안내({N})", "2026-2 학위논문 제출 안내(최종본 제출)")
    assert not explained(f"2026-2 학위논문 제출 안내({N})", "2026-2 학위논문 제출 안내(final)")
    # any other mask stays as it is
    assert not explained("분실물 습득 안내 (연락처 [REDACTED_PHONE])", "분실물 습득 안내 (연락처 사무실)")


def test_comparing_a_long_roster_with_a_rewritten_title_is_fast():
    """A regex with a wildcard per mask backtracked for minutes on these; the walk is linear."""
    import time

    head = "2026 성적우수장학생 선발 결과 명단: "
    spaced, slashed = head + " ".join([N] * 12) + " 발표", head + "/".join([N] * 8) + " 안내"
    # the school rewrote the list behind the same head (the old regex took over 5 s on 30 words, and
    # each word more doubled it)
    rewritten = [head + " ".join(["가나"] * 34) + " 변경", head + "/".join(["가나"] * 40) + " 변경",
                 head + " ".join(["가나다"] * 12) + " 발표 추가", head + " ".join(["가나다라"] * 13) + " 발표",
                 head + "/".join(["가나다"] * 8) + " 안내 (정정)", spaced + " 외", slashed[:-3]]
    started = time.monotonic()
    for old in (spaced, slashed):
        for new in rewritten:
            assert not dispatcher._explained_by_masks(old, new)
    assert time.monotonic() - started < 1.0
    assert dispatcher._explained_by_masks(spaced, spaced.replace(N, "최종본", 1))
    assert dispatcher._explained_by_masks(slashed, slashed.replace(N, "공개용", 1))


# --- S49: a takedown stays down -------------------------------------------------------------------

def test_a_taken_down_notice_is_never_posted_or_edited_again(aws):
    aws["public"].put_item(Item=NOTICE)
    fake = FakeDiscord(lambda: DAY)
    assert run(aws, JOB, fake, DAY, ROLES) == "posted"
    lines = admin.takedown(aws["public"], aws["delivery"], NOTICE["SK"][2:], apply=True, send=fake, token="t",
                           now=DAY + timedelta(hours=1))
    assert lines[-1] == "done" and fake.calls[-1][0] == "DELETE"
    done = len(fake.calls)
    # the post job redelivered (or redriven from the DLQ), a late edit, a repost: nothing goes out
    repost = {**JOB, "action": "repost", "changed": ["title", "updated_date"]}
    for job in (JOB, EDIT, repost):
        assert run(aws, job, fake, DAY + timedelta(hours=2), ROLES) == "removed"
    assert fake.calls[done:] == []
    assert aws["delivery"].get_item(Key=DKEY).get("Item") is None


def test_the_dispatcher_makes_no_job_for_a_removed_notice():
    removed = {**NOTICE, "title": "", "title_key": "", "removed": True}
    records = [stream("MODIFY", removed, old=NOTICE), stream("INSERT", removed),
               stream("MODIFY", {**removed, "category_id": "general"}, old=removed)]
    assert dispatcher.jobs_for(records, date(2026, 11, 6)) == []


# --- S50: the delivery record follows its notice -------------------------------------------------

def _record_ttl(aws) -> int:
    return int(aws["delivery"].get_item(Key=DKEY)["Item"]["ttl"])


def test_an_edit_moves_the_delivery_record_along_with_the_notice(aws):
    aws["public"].put_item(Item={**NOTICE, "ttl": int((DAY + NOTICE_TTL).timestamp())})
    fake = FakeDiscord(lambda: DAY)
    run(aws, JOB, fake, DAY)
    assert _record_ttl(aws) == int((DAY + DELIVERY_TTL).timestamp())

    # 100 days later the school changes it: store.upsert moved the notice's ttl to then + 120 days
    later = DAY + timedelta(days=100)
    notice_ttl = int((later + NOTICE_TTL).timestamp())
    aws["public"].update_item(Key=JOB["key"], UpdateExpression="SET title = :t, #ttl = :ttl",
                              ExpressionAttributeNames={"#ttl": "ttl"},
                              ExpressionAttributeValues={":t": "제목 수정", ":ttl": notice_ttl})
    assert run(aws, EDIT, fake, later) == "edited_replied"
    assert _record_ttl(aws) == notice_ttl + int((DELIVERY_TTL - NOTICE_TTL).total_seconds())

    # a quiet edit moves it too, and never back
    assert run(aws, {**EDIT, "quiet": True}, fake, later + timedelta(days=50)) == "edited_quietly"
    assert _record_ttl(aws) == int((later + timedelta(days=50) + DELIVERY_TTL).timestamp())
    before = _record_ttl(aws)
    discord._keep_record(aws["delivery"], DKEY, {"ttl": 1}, DAY)
    assert _record_ttl(aws) == before


def test_a_record_a_takedown_deleted_is_not_brought_back(aws):
    discord._keep_record(aws["delivery"], DKEY, {"ttl": int(NOW.timestamp())}, NOW)
    assert aws["delivery"].get_item(Key=DKEY).get("Item") is None


def _flip_pin(aws, when: datetime) -> tuple[dict, dict]:
    """What store.upsert does when only `pinned` changes: the item is rewritten and its ttl moves."""
    old = aws["public"].get_item(Key=JOB["key"])["Item"]
    new = {**old, "pinned": not old.get("pinned"), "ttl": int((when + NOTICE_TTL).timestamp())}
    aws["public"].put_item(Item=new)
    return old, new


def test_a_pin_flip_long_after_posting_keeps_the_record_past_the_notice(aws):
    aws["public"].put_item(Item={**NOTICE, "ttl": int((DAY + NOTICE_TTL).timestamp())})
    fake = FakeDiscord(lambda: DAY)
    assert run(aws, JOB, fake, DAY) == "posted"
    posted_calls = len(fake.calls)

    # 100 days later the school unpins it: nothing the message shows changed, but the ttl moved
    later = DAY + timedelta(days=100)
    old, new = _flip_pin(aws, later)
    jobs = dispatcher.jobs_for([stream("MODIFY", new, old=old)], later.date())
    assert jobs == [{"action": "keep", "key": JOB["key"], "channel": "academic"}]
    assert run(aws, jobs[0], fake, later) == "kept"
    assert fake.calls[posted_calls:] == []  # no Discord call
    assert _record_ttl(aws) >= new["ttl"] + int((DELIVERY_TTL - NOTICE_TTL).total_seconds())

    # the day after the record would have expired, it is still there, and a takedown finds the message
    at = DAY + DELIVERY_TTL + timedelta(days=1)
    assert _record_ttl(aws) > int(at.timestamp())
    lines = admin.takedown(aws["public"], aws["delivery"], NOTICE["SK"][2:], apply=True, send=fake, token="t", now=at)
    assert lines[-1] == "done" and fake.calls[-1] == ("DELETE", f"/channels/111/messages/{snowflake(DAY)}", None)


def test_a_keep_job_leaves_a_missing_record_missing_and_never_touches_discord(aws):
    aws["public"].put_item(Item={**NOTICE, "ttl": int((DAY + NOTICE_TTL).timestamp())})
    keep = {"action": "keep", "key": JOB["key"], "channel": "academic"}
    assert run(aws, keep, None, DAY) == "nothing_to_keep"  # send=None: a Discord call would fail
    assert aws["delivery"].query(KeyConditionExpression=Key("PK").eq(DKEY["PK"]))["Items"] == []
    aws["public"].update_item(Key=JOB["key"], UpdateExpression="SET removed = :t", ExpressionAttributeValues={":t": True})
    assert run(aws, keep, None, DAY) == "removed"


def test_a_keep_job_finds_the_record_in_the_channel_the_notice_was_posted_to(aws):
    aws["public"].put_item(Item={**NOTICE, "ttl": int((DAY + NOTICE_TTL).timestamp())})
    fake = FakeDiscord(lambda: DAY)
    run(aws, JOB, fake, DAY)  # CH#academic
    later = DAY + timedelta(days=100)
    aws["public"].update_item(Key=JOB["key"], UpdateExpression="SET #ttl = :ttl", ExpressionAttributeNames={"#ttl": "ttl"},
                              ExpressionAttributeValues={":ttl": int((later + NOTICE_TTL).timestamp())})
    assert run(aws, {"action": "keep", "key": JOB["key"], "channel": "notice"}, fake, later) == "kept"
    assert _record_ttl(aws) == int((later + DELIVERY_TTL).timestamp())


def test_only_a_moved_ttl_makes_a_keep_job():
    base = {**NOTICE, "ttl": 1000}
    records = [
        stream("MODIFY", {**base, "pinned": True}, old=base),  # same ttl: nothing to keep
        stream("MODIFY", {**base, "department": "x", "ttl": 2000}, old=base),
        stream("MODIFY", {**base, "title": "제목 수정", "ttl": 2000}, old=base),  # an edit job keeps it itself
        stream("MODIFY", {**base, "category_id": "general", "pinned": True, "ttl": 2000},
               old={**base, "category_id": "general"}),
    ]
    jobs = dispatcher.jobs_for(records, date(2026, 11, 6))
    assert [(j["action"], j["channel"]) for j in jobs] == [("keep", "academic"), ("edit", "academic"),
                                                            ("keep", "notice")]
    # with notify off, a recategorised notice keeps the record in the channel it was posted to
    moved = stream("MODIFY", {**base, "category_id": "general", "ttl": 2000}, old=base)
    assert dispatcher.jobs_for([moved], date(2026, 11, 6), notify=False) == [
        {"action": "keep", "key": JOB["key"], "channel": "academic"}]
    assert dispatcher.jobs_for([moved], date(2026, 11, 6))[0]["action"] == "edit"


def test_while_notify_is_off_only_keep_jobs_go_out(aws, monkeypatch):
    queue = aws["sqs"].create_queue(QueueName="kw-discord")["QueueUrl"]
    monkeypatch.setattr(dispatcher, "DISCORD_QUEUE_URL", queue)
    base = {**NOTICE, "ttl": 1000}
    records = [stream("INSERT", {**NOTICE, "SK": "N#9"}),
               stream("MODIFY", {**base, "title": "제목 수정", "ttl": 2000}, old=base),
               stream("MODIFY", {**base, "pinned": True}, old=base)]
    assert dispatcher.handler({"Records": records}, None) == {"batchItemFailures": []}
    bodies = [json.loads(m["Body"]) for m in aws["sqs"].receive_message(QueueUrl=queue, MaxNumberOfMessages=10)
              .get("Messages", [])]
    assert bodies == [{"action": "keep", "key": JOB["key"], "channel": "academic"}]


def test_keep_jobs_go_through_without_a_bot_token_or_after_a_429(aws, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("a keep job called Discord")

    monkeypatch.setattr(discordapi.urllib.request, "urlopen", no_network)
    aws["public"].put_item(Item={**NOTICE, "ttl": int((DAY + NOTICE_TTL).timestamp())})
    run(aws, JOB, FakeDiscord(lambda: DAY), DAY)
    before = _record_ttl(aws)
    aws["public"].update_item(Key=JOB["key"], UpdateExpression="SET #ttl = :ttl", ExpressionAttributeNames={"#ttl": "ttl"},
                              ExpressionAttributeValues={":ttl": before})  # the notice now outlives the record
    keep = json.dumps({"action": "keep", "key": JOB["key"], "channel": "academic"})
    # no bot token at all: the post waits for one, the keep job goes through
    event = {"Records": [{"messageId": "m0", "body": json.dumps(JOB)}, {"messageId": "m1", "body": keep}]}
    assert discord.handler(event, None) == {"batchItemFailures": [{"itemIdentifier": "m0"}]}
    assert _record_ttl(aws) >= before + int((DELIVERY_TTL - NOTICE_TTL).total_seconds())

    # a 429 the batch couldn't wait out holds back the Discord jobs after it, not the keep job
    aws["ssm"].put_parameter(Name="/kw/discord/bot-token", Value=TOKEN, Type="SecureString")
    real = discord.handle_job
    seen = []

    def handle(job, *args):
        seen.append(job["action"])
        if job["action"] != "keep":
            raise discordapi.RateLimited("discord 429", 30.0)
        return real(job, *args)

    monkeypatch.setattr(discord, "handle_job", handle)
    event = {"Records": [{"messageId": "m0", "body": json.dumps(JOB)}, {"messageId": "m1", "body": json.dumps(EDIT)},
                         {"messageId": "m2", "body": keep}]}
    assert discord.handler(event, None) == {"batchItemFailures": [{"itemIdentifier": "m0"}, {"itemIdentifier": "m1"}]}
    assert seen == ["post", "keep"]


# --- S51: a source that keeps failing on its own -------------------------------------------------

def test_a_board_that_keeps_failing_on_its_own_needs_a_human(aws, capsys):
    table, key = aws["public"], {"PK": "SRC#dept-ee", "SK": "STATE"}
    raised, state = [], {}
    for reason in ("http_failure", "redirect", "http_failure", "http_failure"):
        politeness.source_failed(table, key, state, NOW, reason, timedelta(hours=2))
        state = table.get_item(Key=key)["Item"]
        out = capsys.readouterr().out
        names = [m["Name"] for d in map(json.loads, out.splitlines()) if "_aws" in d
                 for m in d["_aws"]["CloudWatchMetrics"][0]["Metrics"]]
        raised.append(names.count("NeedsHuman"))
        assert not state["disabled"]
    assert raised == [0, 0, 1, 1]  # from the third failure in a row, every time
    assert [e["event"] for e in log_events(out)] == ["source_failing"]
    assert log_events(out)[0]["source"] == "SRC#dept-ee" and log_events(out)[0]["fails"] == 4

    politeness.source_ok(table, key, state)  # one success starts the count again
    politeness.source_failed(table, key, table.get_item(Key=key)["Item"], NOW, "http_failure", timedelta(hours=2))
    assert "NeedsHuman" not in metric_names(capsys)


def test_hard_blocks_on_one_source_still_switch_it_off_with_one_needs_human(aws, capsys):
    table, key = aws["public"], {"PK": "SRC#kw-job", "SK": "STATE"}
    state = {}
    for _ in range(3):
        politeness.source_failed(table, key, state, NOW, "forbidden", timedelta(hours=2))
        state = table.get_item(Key=key)["Item"]
    assert state["disabled"] is True
    assert metric_names(capsys).count("NeedsHuman") == 1


# --- S53: 긴급 look-alikes and outside notices -----------------------------------------------------

def test_closed_circuits_malls_and_outside_notices_never_ring_urgent():
    calm = ["폐쇄회로(CCTV) 설치 안내", "폐쇄 회로 TV 교체 공사", "폐쇄몰 입점 업체 모집", "폐쇄형 SNS 운영 안내"]
    real = ["화재로 새빛관 폐쇄", "도서관 임시 폐쇄 안내", "정문 폐쇄 및 우회 안내"]
    assert [t for t in calm if routing.is_urgent(t)] == []
    assert [t for t in real if not routing.is_urgent(t)] == []
    assert not routing.urgent_source("external")
    assert routing.urgent_source("general") and routing.urgent_source("academic") and routing.urgent_source("job")


def test_an_outside_exam_change_pings_its_topic_only():
    item = {**NOTICE, "category_id": "external", "title": "OO자격시험 시험장소 변경 안내"}
    assert routing.is_urgent(item["title"])
    roles = {**ROLES, "career": "780"}
    assert discord.mentions_for(item, roles, DAY) == ["780"]
    assert discord.mentions_for({**item, "category_id": "general"}, roles, DAY) == ["778", "779"]


# --- S46 in Discord: titles that may still name someone ----------------------------------------

def test_titles_that_may_still_name_someone_go_out_without_the_title():
    # department boards: thesis, profile and prize titles are about people
    for title in ("2026 학위논문 공개발표 일정", f"졸업생 인터뷰 {N}", "캡스톤디자인 경진대회 최우수상",
                  "공모전 대상 발표", "해커톤 우승", "박사학위 논문 심사 안내"):
        assert routing.name_risk("dept-ee", title), title
    assert not routing.name_risk("dept-ee", "재학생 대상 진로 특강 안내")  # 대상 as "for"
    for title in ("우수논문상 시상 안내", "봉사상 후보 추천 안내", "신인상 시상식 개최", "교육감상 수여식",
                  "감사상 전달식"):
        assert routing.name_risk("dept-ee", title), title
    # any board: a roster or prize title with a word left that looks like a full name
    missed = "2026 우수상 수상을 축하합니다 전자공학과 홍길동"
    assert redact_text(missed) == missed  # too far from the prize for redaction ...
    assert routing.name_risk("general", missed)  # ... so Discord shows the link only
    assert discord.render({**NOTICE, "title": missed}).startswith("**[학사]** 새 글")
    for title in UNCHANGED + NEW_UNCHANGED:  # no school-wide look-alike loses its title
        assert not routing.name_risk("general", title), title


# --- S55: old robots.txt records, robots_skip in www and campus ---------------------------------

def test_stale_robots_counts_kst_days(monkeypatch):
    monkeypatch.setattr(politeness, "ROBOTS", {
        "a.kw.ac.kr": politeness.RobotsCheck("2026-11-06", "no robots.txt (404)"),
        "b.kw.ac.kr": politeness.RobotsCheck("2026-10-01", "no robots.txt (404)"),
    })
    # 180 days after 2026-11-06 is 2027-05-05 (KST)
    assert politeness.stale_robots(datetime(2027, 5, 5, 14, 0, tzinfo=UTC)) == ["b.kw.ac.kr"]  # 23:00 KST
    assert politeness.stale_robots(datetime(2027, 5, 5, 15, 30, tzinfo=UTC)) == ["b.kw.ac.kr", "a.kw.ac.kr"]
    assert politeness.stale_robots(datetime(2026, 11, 6, tzinfo=KST)) == []


def _campus_fetch(calls):
    menu, cal = (FIX / "cafeteria_week.html").read_text(), (FIX / "academic_calendar_2026.html").read_text()

    def fetch(url, form=None):
        calls.append(url)
        return FetchResult(menu if "facility11" in url else cal, None, 200)
    return fetch


def test_an_old_robots_record_needs_a_human_every_campus_run(aws, monkeypatch, capsys):
    aws["ssm"].put_parameter(Name="/kw/collector/enabled", Value="true", Type="String")
    calls = []
    monkeypatch.setattr(campus, "fetch_list_page", _campus_fetch(calls))
    monday = datetime(2026, 11, 2, 22, 30, tzinfo=UTC)
    out = campus.run(monday, aws["public"], clock=lambda: monday, sleep=lambda s: None)
    assert "robots_stale" not in out and "NeedsHuman" not in metric_names(capsys)  # all checked 2026-11-06

    monkeypatch.setitem(politeness.ROBOTS, "ee.kw.ac.kr", politeness.RobotsCheck("2026-01-05", "no robots.txt (404)"))
    out = campus.run(monday, aws["public"], clock=lambda: monday, sleep=lambda s: None)
    text = capsys.readouterr().out
    assert out["robots_stale"] == 1
    stale = [e for e in log_events(text) if e["event"] == "robots_stale"]
    assert stale == [{"event": "robots_stale", "hosts": 1, "oldest": "2026-01-05", "first": "ee.kw.ac.kr"}]
    assert '"NeedsHuman": 1' in text and '"RobotsStale": 1' in text


def test_disallowed_campus_pages_are_skipped_and_reported(aws, monkeypatch, capsys):
    aws["ssm"].put_parameter(Name="/kw/collector/enabled", Value="true", Type="String")
    monkeypatch.setitem(politeness.ROBOTS, "www.kw.ac.kr", politeness.RobotsCheck("2026-11-07", "Disallow: /", ("/",)))
    calls = []
    monkeypatch.setattr(campus, "fetch_list_page", _campus_fetch(calls))
    monday = datetime(2026, 11, 2, 22, 30, tzinfo=UTC)
    out = campus.run(monday, aws["public"], clock=lambda: monday, sleep=lambda s: None)
    assert out == {"status": "ok", "cafeteria": "robots", "calendar": "robots"} and calls == []
    skips = [e["source"] for e in log_events(capsys.readouterr().out) if e["event"] == "robots_skip"]
    assert skips == ["campus-cafeteria", "campus-calendar"]


def test_a_disallowed_www_board_is_skipped_and_reported_and_the_other_goes_on(aws, monkeypatch, capsys):
    job, faq = wwwboards.BOARDS
    monkeypatch.setitem(politeness.ROBOTS, "www.kw.ac.kr",
                        politeness.RobotsCheck("2026-11-07", "Disallow: /ko/life/job.jsp", ("/ko/life/job.jsp",)))
    calls = []

    def fetch(url, **kw):
        calls.append(url)
        return FetchResult((FIX / "www_faq_list.html").read_text(), None, 200)

    out = wwwboards.collect(NOW, aws["public"], fetch=fetch, clock=lambda: NOW, sleep=lambda s: None)
    assert out["board"] == faq.source and out["status"] == "ok" and calls == [faq.list_url]
    text = capsys.readouterr().out
    assert [e["source"] for e in log_events(text) if e["event"] == "robots_skip"] == [job.source]
    assert '"NeedsHuman": 1' in text


# --- S60: 429s ---------------------------------------------------------------------------------------

def _http_429(headers: dict, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://discord.com/api/v10/x", 429, "Too Many Requests", headers, io.BytesIO(body))


def test_retry_after_comes_from_the_body_or_the_headers():
    assert discordapi._retry_after(_http_429({}, b'{"retry_after": 0.25, "global": false}')) == 0.25
    # Cloudflare's 429 in front of Discord has an HTML body: the header still says how long
    assert discordapi._retry_after(_http_429({"Retry-After": "7"}, b"<html>429 Too Many Requests</html>")) == 7.0
    assert discordapi._retry_after(_http_429({"X-RateLimit-Reset-After": "2.5"}, b"")) == 2.5
    assert discordapi._retry_after(_http_429({"Retry-After": "nan"}, b"[1, 2]")) == 1.0
    assert discordapi._retry_after(_http_429({"Retry-After": "-3"}, b"not json")) == 1.0


class Lambda:
    def __init__(self, seconds: float):
        self.seconds = seconds

    def get_remaining_time_in_millis(self) -> int:
        return int(self.seconds * 1000)


ARN = "arn:aws:sqs:ap-northeast-2:123456789012:kw-discord"


def _records(n: int) -> list[dict]:
    return [{"messageId": f"m{i}", "receiptHandle": f"rh{i}", "eventSourceARN": ARN, "body": json.dumps(JOB)}
            for i in range(n)]


class FakeSqs:
    def __init__(self):
        self.calls = []

    def change_message_visibility_batch(self, QueueUrl, Entries):
        self.calls.append((QueueUrl, Entries))
        return {}


def _sender(aws, monkeypatch, outcomes) -> tuple[list, FakeSqs]:
    """handle_job answers from `outcomes` (an exception is raised); SQS calls are recorded."""
    aws["ssm"].put_parameter(Name="/kw/discord/bot-token", Value=TOKEN, Type="SecureString")
    tries, sqs = [], FakeSqs()

    def handle(*args):
        tries.append(args[0])
        outcome = outcomes[min(len(tries), len(outcomes)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(discord, "handle_job", handle)
    monkeypatch.setattr(discord, "boto3", SimpleNamespace(resource=boto3.resource, client=lambda name: sqs))
    return tries, sqs


def test_a_short_429_is_waited_out_in_place(aws, monkeypatch):
    tries, sqs = _sender(aws, monkeypatch, [discordapi.RateLimited("discord 429", 0.4), "posted"])
    slept = []
    assert discord.handler({"Records": _records(2)}, Lambda(50), sleep=slept.append) == {"batchItemFailures": []}
    assert slept == [0.4] and len(tries) == 3 and sqs.calls == []


def test_a_long_429_sends_the_batch_back_for_when_discord_is_ready(aws, monkeypatch):
    tries, sqs = _sender(aws, monkeypatch, [discordapi.RateLimited("discord 429", 30.2)])
    slept = []
    out = discord.handler({"Records": _records(3)}, Lambda(50), sleep=slept.append)
    assert out == {"batchItemFailures": [{"itemIdentifier": f"m{i}"} for i in range(3)]}
    assert len(tries) == 1 and slept == []  # the rest of the batch never hit Discord
    assert sqs.calls == [("https://sqs.ap-northeast-2.amazonaws.com/123456789012/kw-discord",
                          [{"Id": str(i), "ReceiptHandle": f"rh{i}", "VisibilityTimeout": 31} for i in range(3)])]


def test_no_waiting_without_the_time_for_it_and_a_bounded_number_of_waits(aws, monkeypatch):
    tries, sqs = _sender(aws, monkeypatch, [discordapi.RateLimited("discord 429", 1.0)])
    slept = []
    discord.handler({"Records": _records(1)}, Lambda(15), sleep=slept.append)  # 15 s left: no wait
    assert slept == [] and len(tries) == 1 and sqs.calls[0][1][0]["VisibilityTimeout"] == 1

    tries.clear()
    discord.handler({"Records": _records(1)}, Lambda(50), sleep=slept.append)  # Discord keeps saying wait
    assert slept == [1.0] * (discord.RATE_LIMIT_TRIES - 1) and len(tries) == discord.RATE_LIMIT_TRIES


def test_a_429_on_the_post_frees_the_claim_so_the_retry_posts(aws):
    aws["public"].put_item(Item=NOTICE)
    fake = FakeDiscord(lambda: DAY)
    fake.fail = discordapi.RateLimited("discord 429", 0.5)
    with pytest.raises(discordapi.RateLimited):
        run(aws, JOB, fake, DAY, ROLES)
    fake.fail = None
    # a second later (the in-place wait), not after the 90 s a dead attempt's claim needs
    assert run(aws, JOB, fake, DAY + timedelta(seconds=1), ROLES) == "posted"
    assert [m for m, _, _ in fake.calls] == ["POST"]
    assert fake.calls[0][2]["allowed_mentions"] == {"parse": [], "roles": ["777"]}  # its ping slot kept


def test_a_429_on_the_edit_note_frees_todays_slot(aws):
    aws["public"].put_item(Item=NOTICE)
    fake = FakeDiscord(lambda: DAY)
    run(aws, JOB, fake, DAY)
    retitle(aws, "제목 수정")

    class LimitedReply(FakeDiscord):
        def __call__(self, method, path, token, body=None, timeout=10):
            if method == "POST":
                raise discordapi.RateLimited("discord 429", 0.5)
            return super().__call__(method, path, token, body, timeout)

    when = DAY + timedelta(hours=1)
    with pytest.raises(discordapi.RateLimited):
        run(aws, EDIT, LimitedReply(lambda: when), when)
    assert run(aws, EDIT, fake, when + timedelta(seconds=1)) == "edited_replied"
    assert sum(1 for m, _, b in fake.calls if m == "POST" and "message_reference" in b) == 1


def test_the_sender_waits_out_a_cloudflare_429_end_to_end(aws, monkeypatch):
    """The real handle_job and discordapi.call: a 429 with an HTML body and Retry-After, then the post."""
    aws["public"].put_item(Item=NOTICE)
    aws["ssm"].put_parameter(Name="/kw/discord/bot-token", Value=TOKEN, Type="SecureString")
    aws["ssm"].put_parameter(Name="/kw/discord/channels", Value='{"academic": "111"}', Type="String")
    sent = []

    def urlopen(request, timeout, context):
        sent.append(request.get_method())
        if len(sent) == 1:
            raise _http_429({"Retry-After": "1"}, b"<html>error code: 1015</html>")
        return io.BytesIO(json.dumps({"id": "1234567890123456789"}).encode())

    monkeypatch.setattr(discordapi.urllib.request, "urlopen", urlopen)
    slept = []
    event = {"Records": [{"messageId": "m0", "body": json.dumps(JOB)}]}
    assert discord.handler(event, Lambda(50), sleep=slept.append) == {"batchItemFailures": []}
    assert sent == ["POST", "POST"] and slept == [1.0]
    record = aws["delivery"].get_item(Key=DKEY)["Item"]
    assert record["status"] == "sent" and record["message_id"] == "1234567890123456789"
