"""Redaction v2: names in titles, wider contact patterns, and no mangled ordinary titles."""

from pathlib import Path

import pytest

from kwcampus import aicon, deptboards, wixfeeds, wwwboards
from kwcampus.kwnotice.list_parser import parse_list_soup
from kwcampus.kwnotice.parsing_shared import boundary_or_issue, soup_from_boundary
from kwcampus.kwnotice.redaction import (
    NAME,
    REDACTION_LEVEL,
    REDACTION_VERSION,
    redact_human_fields,
    redact_text,
)

N = NAME
FIXTURES = Path(__file__).parent / "fixtures"

NAMES = [
    # names next to a role word
    ("[축하] 전자공학과 홍길동 학생 대상 수상 / 학번 2021123456",
     f"[축하] 전자공학과 {N} 학생 대상 수상 / 학번 [REDACTED_IDENTIFIER]"),
    ("[수상] 박영희 학생 IEEE 학술대회 최우수논문상 수상", f"[수상] {N} 학생 IEEE 학술대회 최우수논문상 수상"),
    ("전자공학과 김민준 학생 캡스톤디자인 우수상", f"전자공학과 {N} 학생 캡스톤디자인 우수상"),
    ("로봇학부 최지훈 교수님 정년퇴임 기념 강연", f"로봇학부 {N} 교수님 정년퇴임 기념 강연"),
    ("김수정 교수 연구팀, 네이처 논문 게재", f"{N} 교수 연구팀, 네이처 논문 게재"),
    ("이지원 학생의 창업 이야기", f"{N} 학생의 창업 이야기"),
    ("홍길동 동문 초청 특강 안내", f"{N} 동문 초청 특강 안내"),
    ("남궁민수 교수 연구팀 논문 게재", f"{N} 교수 연구팀 논문 게재"),
    ("홍길동님 감사합니다", f"{N}님 감사합니다"),
    ("김철수씨 분실물 찾아가세요", f"{N}씨 분실물 찾아가세요"),
    ("홍길동, 김철수 학생 창업경진대회 대상 수상", f"{N}, {N} 학생 창업경진대회 대상 수상"),
    ("소프트웨어학부 이영희 외 3명 논문 게재", f"소프트웨어학부 {N} 외 3명 논문 게재"),
    ("[부고] 행정지원팀 김철수 선생님 빙모상", f"[부고] 행정지원팀 {N} 선생님 빙모상"),
    # rosters, award lists, thesis schedules
    ("2026학년도 2학기 성적우수장학생 선발 결과 안내 (홍길동, 김철수, 이영희)",
     f"2026학년도 2학기 성적우수장학생 선발 결과 안내 ({N}, {N}, {N})"),
    ("2026 후기 석사학위논문 공개발표 안내(김철수, 이영희)", f"2026 후기 석사학위논문 공개발표 안내({N}, {N})"),
    ("박사학위논문 공개발표 안내 (김철수)", f"박사학위논문 공개발표 안내 ({N})"),
    ("2026 우수논문상 수상자: 김민준, 이서연", f"2026 우수논문상 수상자: {N}, {N}"),
    ("합격자 명단: 2021123456 홍길동", f"합격자 명단: [REDACTED_ACCOUNT] {N}"),
    # obituaries
    ("[부고] 전자공학과 김철수 교수 부친상", f"[부고] 전자공학과 {N} 교수 부친상"),
    ("[부고] 홍길동(전자공학과) 부친상", f"[부고] {N}(전자공학과) 부친상"),
    # partly hidden names
    ("2026-2 성적우수장학생 선발 결과 (김OO 외)", f"2026-2 성적우수장학생 선발 결과 ({N} 외)"),
    ("홍*동 학생 연락 바랍니다", f"{N} 학생 연락 바랍니다"),
    ("김○수, 이O진 학생 수상", f"{N}, {N} 학생 수상"),
]

CONTACTS = [
    ("분실물 습득 안내 (연락처 010.1234.5678)", "분실물 습득 안내 (연락처 [REDACTED_PHONE])"),
    ("문의: 02.940.5114 학생복지팀", "문의: [REDACTED_PHONE] 학생복지팀"),
    ("문의 (010)1234-5678", "문의 [REDACTED_PHONE]"),
    ("문의 hong [at] kw.ac.kr", "문의 [REDACTED_EMAIL]"),
    ("문의 hong(at)kw(dot)ac(dot)kr", "문의 [REDACTED_EMAIL]"),
    ("문의 hong＠kw.ac.kr", "문의 [REDACTED_EMAIL]"),
    ("공지 2021123456 학생 확인 요망", "공지 [REDACTED_ACCOUNT] 학생 확인 요망"),
    ("분실 학생증 2021****56 찾아가세요", "분실 학생증 [REDACTED_IDENTIFIER] 찾아가세요"),
    ("수험번호 12345678 확인", "수험번호 [REDACTED_IDENTIFIER] 확인"),
    ("+82.10.1234.5678로 연락", "[REDACTED_PHONE]로 연락"),
]

UNCHANGED = [
    "교수학습센터 안내", "학생복지팀 공지", "국가장학금 대상자 안내", "외국인 학생 대상 한국어 교육",
    "2026학년도 신입 학생 오리엔테이션", "장애 학생 지원 안내", "우수 학생 선발 안내", "지도교수 면담 안내",
    "명예교수 초청 특강", "교환학생 모집 안내", "재학생 대상 설문조사", "학생회관 공사 안내",
    "교수님과 함께하는 진로 상담", "선생님 공지", "선배님 멘토링", "2026 수상자 발표 및 시상식 개최",
    "합격자 발표 및 등록 안내", "2027학년도 일본대학 파견 교환학생 선발결과 및 추가서류 제출안내",
    "[대전광역시] 2026년 하반기 인재육성(성취) 장학생 선발 안내(기간연장)", "대학원 학생 연구비 지원",
    "전체 학생 대상 안전교육", "학과 조교 채용 공고", "박사 학위 청구 논문 심사 일정", "공개발표 일정 안내(수정)",
    "장학생 선발 결과 안내(마감)", "2026년 수시모집 최초합격자 발표", "부모님께 드리는 편지", "총장님 신년사",
    "강사님 모집", "박사님 특강", "대상자 확인 및 서류 제출 안내", "[필독] 2026-2학기 전공 지원 안내",
    "해외 교환 학생 귀국 보고회", "현장실습 학생 보험 안내", "졸업 예정 학생 안내", "근로 학생 모집",
    "정보융합학부 학생 대상 특강", "이번 주 학생 식당 운영", "2026학년도 1학기 성적 우수 장학생 명단 발표",
    "마음씨 고운 학생", "제O회 학술제", "OX퀴즈 대회", "2026.10.12 행사", "61.111.8.4 서버 점검",
    "공지번호 2026001234 확인", "공지 제2026001234호", "학번 미기재자 안내", "[부고] 본교 명예교수 별세",
    "2026학년도 1학기 국가장학금 신청자 명단 확인 안내", "선정자 명단 (전공별)", "[수상] 소프트웨어학부 학생팀 대상",
    "(재공지) 2026학년도 동계 계절학기 수강신청 안내", "[학사] 2026-2 성적 확인 및 정정 기간",
    "KLAS 시스템 점검 안내 (11/7 02:00~06:00)", "새빛관 정전 안내", "도서관 휴관 안내",
]


@pytest.mark.parametrize(("title", "expected"), NAMES + CONTACTS)
def test_names_and_contacts_are_masked(title, expected):
    assert redact_text(title) == expected


@pytest.mark.parametrize("title", UNCHANGED)
def test_ordinary_titles_stay_as_they_are(title):
    assert redact_text(title) == title


def test_version_and_level():
    assert REDACTION_VERSION == "v2" and REDACTION_LEVEL == 2


def test_departments_and_human_fields_go_through_the_same_pass():
    fields = redact_human_fields(title="홍길동 학생 수상", category_name="학사", department="학생복지팀", body="")
    assert fields.title == f"{N} 학생 수상" and fields.department == "학생복지팀"


def test_no_fixture_title_is_mangled():
    """Every title in the saved real pages (main board, job, FAQ, departments, aicon, Wix) parses to
    itself: none holds a name, so v2 must change none of them."""
    boards = {b.key: b for b in deptboards.BOARDS}
    titles = []
    for key in ("ee", "law", "biz", "chemng", "sports"):
        titles += [r.title for r in deptboards.parse_board((FIXTURES / f"dept_{key}_list.html").read_text(), boards[key])]
    english = (FIXTURES / "dept_english_list.html").read_bytes().decode("cp949", "replace")
    titles += [r.title for r in deptboards.parse_board(english, boards["english"])]
    titles += [r.title for r in aicon.parse_widget((FIXTURES / "aicon_main.html").read_text())]
    for feed in wixfeeds.FEEDS:
        titles += [r.title for _, r in wixfeeds.parse_feed((FIXTURES / f"wix_{feed.key}_feed.xml").read_text(), feed)]
    for board, name in zip(wwwboards.BOARDS, ("www_job_list.html", "www_faq_list.html"), strict=True):
        titles += [r.title for r in wwwboards.parse_board((FIXTURES / name).read_text(), board)]
    boundary, _ = boundary_or_issue((FIXTURES / "board_list_live_shape_sanitized.html").read_text())
    titles += [r.title for r in parse_list_soup(soup_from_boundary(boundary)).records]
    assert len(titles) > 40
    assert [t for t in titles if "REDACTED_NAME" in t] == []


def test_collectors_mask_names_before_storing():
    ee = next(b for b in deptboards.BOARDS if b.key == "ee")
    page = (FIXTURES / "dept_ee_list.html").read_text().replace("수강정정기간 관련 공지", "[부고] 전자공학과 김철수 교수 부친상")
    title = next(r.title for r in deptboards.parse_board(page, ee) if r.duid == "632")
    assert title == f"[부고] 전자공학과 {N} 교수 부친상"
