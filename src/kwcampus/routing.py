"""Which Discord channel a category goes to, the label users see for it, and its subscription topic."""

from __future__ import annotations

import re

from kwcampus import deptboards, wixfeeds, wwwboards
from kwcampus.kwnotice.redaction import may_name_someone
from kwcampus.topics import TOPIC_BY_KEY, TOPICS, URGENT, Topic

__all__ = ["CATEGORY_LABEL", "CATEGORY_TO_CHANNEL", "COLLEGE_OF", "TOPICS", "TOPIC_BY_KEY", "URGENT", "Topic", "channel_for",
           "is_urgent", "name_risk", "topic_for", "urgent_source"]

# channel keys match the keys of the JSON in the /kw/discord/channels parameter
CHANNEL_KEYS = ("notice", "academic", "scholarship", "career", "international", "department")

CATEGORY_TO_CHANNEL: dict[str, str] = {
    "general": "notice",
    "student": "notice",
    "volunteer": "notice",
    "admissions": "notice",
    "facilities": "notice",
    "military": "notice",
    "academic": "academic",
    "registration-scholarship": "scholarship",
    "external": "career",
    "international-exchange": "international",
    "international-student": "international",
    "dept-aicon": "department",  # 인공지능융합대학 (falls back to "notice" until a channel is set)
    **{b.category_id: "department" for b in (*deptboards.BOARDS, *wixfeeds.FEEDS) if b.category_id.startswith("dept-")},
    "job": "career",  # 취업게시판
    # School offices' and funded programs' own boards (deptboards, office=True):
    # 반도체특성화대학사업단 and HUSS 사업단 pick students of every major for their tracks and courses
    # (양성인원 선발, 계절학기 학점 교류, 교과목 수강 모집, 1학점 프로그램): 학사.
    "office-semi": "academic",
    "office-huss": "academic",
    # 일반대학원's 교학팀 board: 학위논문, 졸업, 입학, 장학 of graduate students, the school's 학사 for them.
    "office-grad": "academic",
    # 한국어교육센터 (outside events and courses for foreign students), 인권센터 (특강, the 폭력예방교육 every
    # student must take, outside campaigns), 대학혁신사업단 (contests, surveys, events) and 창업지원단 (mostly
    # other schools' and districts' startup programs) post nothing about classes or credits: 일반.
    "office-kwiie": "notice",
    "office-humanrights": "notice",
    "office-inno": "notice",
    "office-startup": "notice",
    "office-career": "career",  # 경력개발팀: 채용설명회, 취업박람회, 직무특강
    "office-oia": "international",  # 국제처's notices for international students
}
OFFICE_CATEGORIES = frozenset(b.category_id for b in deptboards.BOARDS if b.office)
# Searchable but never announced: FAQ entries are reference pages, not news.
QUIET_CATEGORIES = frozenset({"faq"})

CATEGORY_LABEL: dict[str, str] = {
    "general": "일반",
    "academic": "학사",
    "student": "학생",
    "volunteer": "봉사",
    "registration-scholarship": "등록/장학",
    "admissions": "입학",
    "facilities": "시설",
    "military": "병무",
    "external": "외부",
    "international-exchange": "국제교류",
    "international-student": "국제학생",
    "dept-aicon": "인공지능융합대학",
    **{b.category_id: b.name for b in (*deptboards.BOARDS, *wixfeeds.FEEDS)},
    **{b.category_id: b.name for b in wwwboards.BOARDS},
}


def channel_for(category_id: str) -> str | None:
    """None: the category is never announced."""
    if category_id in QUIET_CATEGORIES:
        return None
    return CATEGORY_TO_CHANNEL.get(category_id, "notice")

# Department boards and feeds by college: key (category "dept-<key>") → college topic.
# A new board or feed needs a line here (tests/test_routing.py fails until it has one).
COLLEGE_OF: dict[str, str] = {
    **dict.fromkeys(("ei", "ee", "elcomm", "radiowave", "electric", "electricjob", "snme", "semicon"), "ei"),
    "aicon": "aicon",  # the college home widget, all four schools
    **dict.fromkeys(("archi", "chemng", "architecture"), "eng"),  # 건축공학과, 화학공학과, 건축학과
    "sports": "natsci",  # 스포츠융합과학과
    **dict.fromkeys(("chss", "korean", "english", "psy", "dnaci", "media"), "chss"),
    **dict.fromkeys(("law", "kwpa"), "law"),  # 법학부, 행정학과
    "biz": "biz",
    **dict.fromkeys(("kwlife", "kwlaw", "kwgame", "kwsports"), "kwlife"),
    **dict.fromkeys(("ingenium", "liberal"), "ingenium"),  # 인제니움학부대학, 자율전공학부
}
# The other topics are the channels themselves; the 학교-공지 channel is the "general" topic.
_CHANNEL_TOPIC = {"notice": "general", "academic": "academic", "scholarship": "scholarship",
                  "career": "career", "international": "international"}


def topic_for(category_id: str) -> str | None:
    """None: the category is never announced, so nobody can subscribe to it."""
    channel = channel_for(category_id)
    if channel is None:
        return None
    if channel == "department":
        return COLLEGE_OF.get(category_id.removeprefix("dept-"))
    return _CHANNEL_TOPIC.get(channel)


# What students most need to hear at once: cancelled or moved classes and exams, closed buildings,
# outages. Only school-wide sources ring 긴급 (urgent_source); a department's 휴강 or room change rings
# that college's role only. Look-alikes stay out: 출입국, 장애인/장애학생, 내부통제·정보통제·감염병 통제,
# 출입통제시스템, 긴급생활비 (a grant), 단수전공, 정전기, 폐쇄회로 (CCTV)·폐쇄몰·폐쇄형, and planned or
# paperwork posts (모집, 신청, 제출, 계획서, 교원용, 연휴, 개교기념일, 정기점검).
_URGENT = re.compile(
    r"\[긴급\]|긴급\s*(?:공지|안내|알림)|휴강|결강|휴교|폐쇄(?!\s*회로|몰|형)|비대면\s*(?:수업\s*)?전환|정전(?!기)"
    r"|단수(?!\s*전공)"
    r"|(?:강의|수업)\s*(?:실|요일|시간|장소)\s*변경|(?:시험|고사)\s*(?:실|장소|시간|일정)\s*변경|고사실\s*변경"
    r"|(?:출입|차량|교통|도로|통행)\s*(?:통제|제한|금지)(?!\s*시스템)"
    r"|(?:시스템|서비스|전산|네트워크|인터넷|와이파이|wi-?fi|접속)\s*(?:장애(?!인|학생)|중단|불가|오류)"
    r"|(?:운행|운영)\s*중단",
    re.IGNORECASE,
)
_NOT_URGENT = re.compile(r"모집|신청|제출|계획서|교원용|연휴|개교기념일|정기\s*점검")


def is_urgent(title: str) -> bool:
    return bool(_URGENT.search(title)) and not _NOT_URGENT.search(title)


# The main board's 외부 category carries outside organisations' notices (a certificate exam moved to
# another hall, a closed exhibition): they are not about our campus, so they never ring 긴급. Nor do the
# offices' and programs' own boards: a moved class or closed room there concerns one program's students.
_NOT_URGENT_SOURCES = frozenset({"external"}) | OFFICE_CATEGORIES


def urgent_source(category_id: str) -> bool:
    """긴급 is for school-wide notices about our campus (the main board but its 외부 category, the job
    board), not department boards, feeds or the offices' own boards."""
    channel = channel_for(category_id)
    return channel is not None and channel != "department" and category_id not in _NOT_URGENT_SOURCES


# Titles are masked before they are stored (redaction v3: names next to roles, in rosters, award lists,
# thesis schedules and obituaries), but no rule set catches every name. So a title that may still carry
# one goes to Discord without the title and without a mention ("새 글", link only):
# - on department boards and feeds, every award, roster, obituary, pass, selection, thesis and
#   profile title (they are mostly about people);
# - on any board, a roster, prize or pass title that still has a word looking like a full name
#   (redaction.may_name_someone), and any title with a partly hidden name like 홍*동 or 김○○.
_NAME_LIST = re.compile(
    r"수상|명단|합격|부고|조의|당선|선발\s*결과|축하|학위\s*논문|공개\s*발표|심사|졸업생|인터뷰|입상|입선|우승"
    r"|(?:금|은|동|장려|우수|최우수|특별|공로|총장|학장|논문|봉사|감사|신인|교육감)상"
    r"|(?:대회|공모전|경연)\s*대상(?!자)"
)
# a syllable starting a word, then one or two masks: 홍*동, 김**, 이○○ (not 필독*, not 제○회)
_MASKED_NAME = re.compile(r"(?<![가-힣])[가-힣][*＊○◯●]{1,2}(?![회차기호])")


def name_risk(category_id: str, title: str) -> bool:
    if _MASKED_NAME.search(title) or may_name_someone(title):
        return True
    return channel_for(category_id) == "department" and bool(_NAME_LIST.search(title))
