"""Versioned deterministic redaction for human-authored notice fields.

v1 masks e-mail addresses, phone numbers, resident IDs, labelled IDs and account-like digit runs.
v2 (kw-mcp/docs/aws-architecture.md 4-4) widens those (dotted and bracketed phone numbers,
"[at]" addresses, 공지 next to a student number) and masks people's names. v3 masks the names v2
let through (below), takes "02)940-5114" and Unicode dashes again (v1 did), and leaves its own
masks alone, so a second pass changes nothing ("학번 [REDACTED_IDENTIFIER]" stays as it is).

Names:
- a name-like word right before 님/씨/학생/교수/선생님/조교/동문/박사/석사/학우 or 외 N명, also with
  its affiliation in brackets in between ("홍길동(전자공학과) 학생", "김민준(전자공학과) 외 2명"),
  and every name-like word listed with commas or spaces right before it ("홍길동, 김철수 학생");
- a full name (three syllables, or a two-syllable surname and more) right before 외 ("홍길동 외",
  "(김민준 외)"; in brackets any name-like word: "(김산 외)"), or before a prize or a pass
  ("홍길동 최우수상", "김철수 합격", "홍길동 대상 수상", "박영희(경영학부) 대상 수상");
- a name-like word next to a masked ID ("[REDACTED_ACCOUNT] 홍길동");
- roster and award titles (합격자, 명단, 선발 결과, 수상자, 우승자, 장학생, 대상자, 학위논문, 공개발표, and
  prizes ending in 상: 금상, 최우수상, 총장상, 우수논문상, 봉사상 ...): two or more name-like words in a
  row after the first such word or in brackets, one name-like word alone in brackets, and a full
  name right after such a word, past separators, other title words and the person's department
  or year ("공개발표 안내 - 홍길동", "[수상] 전자공학과 4학년 홍길동", "대상 홍길동, 금상 김철수 이영희");
- after a bare 심사, 졸업생, 인터뷰 or 우승 (mostly ordinary titles: "졸업생 진로 현황 조사"), only a
  full name right after it and full names in brackets ("졸업생 인터뷰 홍길동", "인터뷰 (김철수)");
- obituaries (부고, 별세, 부친상 ...): every name-like word;
- partly hidden names: 홍*동, 김○수, 이O진, 김OO.

A name-like word is 2-4 Hangul syllables starting with a common surname, not ending like an
office or a thing (과, 팀, 센터, 학, 생, 사 ...) and not a stopword. So "교수학습센터 안내",
"학생복지팀", "국가장학금 대상자 안내", "외국인 학생 대상", "동영상 강의" and "평점 3.0 이상" stay
as they are. A word ending in 상 counts as a prize unless it ends like a common word (이상, 영상,
현상, 향상, 옥상, 항상, 시스템상, 절차상 ...); a known prize (논문상, 봉사상, 신인상, 교육감상 ...)
counts even so. A bare 대상 is mostly "for" (재학생 대상): it is a prize only before 수상/받/차지/영예,
right after a contest (경진대회 대상, 공모전 대상) or next to another prize (대상 홍길동, 금상 김철수).
"""

import re
from dataclasses import dataclass
from typing import Final

from kwcampus.kwnotice.normalization import normalize_text

REDACTION_VERSION: Final[str] = "v3"
# Stored on every notice item as norm_v: a MODIFY that only raises it is our re-normalisation.
REDACTION_LEVEL: Final[int] = 3
NAME: Final[str] = "[REDACTED_NAME]"

# ASCII hyphen, dot and space, the Unicode dashes (‐ ‑ ‒ – — ― −) and their fullwidth forms
_SEP = r"[-. ‐-―−﹣－．]"
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+[@＠][\w-]+(?:\.[\w-]+)+(?![\w.-])")
# "hong [at] kw.ac.kr", "hong(at)kw(dot)ac(dot)kr", "hong 골뱅이 kw.ac.kr"
_EMAIL_SPELLED = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\}|골뱅이)\s*[A-Za-z0-9-]+"
    r"(?:\s*(?:\.|\[\s*dot\s*\]|\(\s*dot\s*\))\s*[A-Za-z0-9-]+)+",
    re.IGNORECASE,
)
_RESIDENT = re.compile(r"(?<!\d)\d{6}[- ]\d{7}(?!\d)")
# "(010)1234-5678" and "010)1234-5678" lose their brackets; "(010.1234.5678)" keeps them
_KOREAN_MOBILE = re.compile(rf"(?<!\d)(?:\(?01[016789]\)|01[016789]){_SEP}?\d{{3,4}}{_SEP}?\d{{4}}(?!\d)")
_KOREAN_AREA_CODE = (
    r"(?:02|031|032|033|041|042|043|044|051|052|053|054|055|061|062|063|064)"
)
# "(02)940-5114", "02)940-5114" (a bracket left open, as v1 took it) and "02.940.5114"
_KOREAN_LANDLINE = re.compile(
    rf"(?<!\d)(?:\(?{_KOREAN_AREA_CODE}\)|{_KOREAN_AREA_CODE}){_SEP}?\d{{3,4}}{_SEP}?\d{{4}}(?!\d)"
)
_KOREAN_SERVICE = re.compile(
    rf"(?<!\d)(?:1[568]\d{{2}}{_SEP}\d{{4}}|0(?:50|60|70|80){_SEP}?\d{{3,4}}{_SEP}\d{{4}})(?!\d)"
)
_INTERNATIONAL_PREFIX = rf"(?<!\d)\+\d{{1,3}}{_SEP}?\(?\d{{1,4}}\)?"
_INTERNATIONAL_SUFFIX = rf"(?:{_SEP}?\d{{3,4}}){{2}}(?!\d)"
_INTERNATIONAL_PHONE = re.compile(f"{_INTERNATIONAL_PREFIX}{_INTERNATIONAL_SUFFIX}")
_LABELLED_PREFIX = r"(?P<label>학번|사번|교번|학생\s?번호|수험\s?번호|계좌|주민등록번호)"
_LABELLED_SEPARATOR = r"(?P<separator>\s*(?:번호\s*)?[:：=#(\[-]?\s*)"
_LABELLED_VALUE = r"(?P<value>[A-Za-z0-9][A-Za-z0-9 -]{1,18}[A-Za-z0-9])"  # keeps the space after it
# never our own mask: "학번 [REDACTED_IDENTIFIER]" must not become "[[REDACTED_IDENTIFIER]_IDENTIFIER]"
_LABELLED = re.compile(f"{_LABELLED_PREFIX}{_LABELLED_SEPARATOR}(?!REDACTED_){_LABELLED_VALUE}")
_ACCOUNT_RUN = re.compile(r"(?<![\d-])\d{10,14}(?![\d-])")
# A student number with some digits hidden: 2021****56, 20211234**
_PARTIAL_ID = re.compile(r"(?<![\d*＊])(?:19|20)\d{2}[\d*＊xX]{4,8}(?![\d*＊xX])")
# Only a notice's own number survives: "공지번호 2026001234", "공지 제2026001234호"
_NOTICE_NUMBER_BEFORE = re.compile(r"(?:공지\s*번호|notice\s*(?:no\.?|#))\s*[:#]?\s*$|제\s*$", re.IGNORECASE)

# Surnames of about 99% of people in Korea (2015 census), most common first.
_SURNAMES: Final[frozenset[str]] = frozenset(
    "김이박최정강조윤장임한오서신권황안송전홍유고문양손배백허남심노하곽성차주우구민류나진지엄채원천방"
    "공현함변염여추도소석선설마길연위표명기반라왕금옥육인맹제모탁국어은편용예경봉"
)
_COMPOUND_SURNAMES: Final[frozenset[str]] = frozenset({"남궁", "제갈", "선우", "황보", "독고", "사공", "서문"})
# Last syllables of offices, places, events and groups; few given names end this way.
_NOT_NAME_ENDINGS: Final[frozenset[str]] = frozenset("과부대팀실처단학생장청회터관소사층님씨")
# Words that start with a surname syllable but are no name (title words, ranks, groups, awards).
_STOPWORD_TEXT = """
    안내 공고 공지 발표 확인 조회 결과 최종 추가 변경 일정 등록 신청 모집 선발 접수 마감 연장 정정 수정 재공지
    기간 기한 시간 장소 방법 대상 해당 관련 개최 실시 시행 운영 진행 참여 참가 지원 계획 예정 완료 취소 연기
    명단 합격 합격자 최종합격 당선 당선자 수상 수상자 입상 입상자 선정 선정자 장학 장학금 대상자 선발자 예비
    부고 별세 소천 타계 영면 추모 조문 빈소 발인 장례 장례식 부친상 모친상 빙부상 빙모상 시부상 시모상 조부상
    조모상 외조부상 외조모상 장인상 장모상 본인상 배우자상 형제상 자녀상 남편상 부인상 삼가 고인 명복 애도
    축하 경축 시상 시상식 수여 수여식 표창 포상 감사 감사패 공로 공로상 우수 최우수 우수상 최우수상 장려 장려상
    대상 금상 은상 동상 특별상 인기상 입선 가작 우승 준우승 수석 차석 성적 성적우수 학업 학위 학위수여
    신입 신규 기존 전체 일반 정규 국제 국내 해외 한국 전공 복수전공 부전공 연계전공 융합전공 소속 모든 각종
    여자 남자 남녀 여성 남성 장애 고등 정시 수시 편입 재입학 제적 유예 수료 이수 미이수 휴학 복학 졸업 졸업예정
    지도 지도교수 담당 전임 신임 정년 석좌 명예 명예교수 초빙 겸임 조교 조교수 부교수 정교수 주임 연구 연구원
    교직원 구성원 직원 위원 위원장 회원 동문 선배 후배 선생 학우 고객 부모 학부모 주인 어머 아버 마음 여러분
    교육 강의 강좌 수업 시험 평가 과제 논문 심사 공개 발표회 설명회 박람회 경진 경진대회 공모 공모전 대회 행사
    특강 강연 세미나 워크숍 캠프 축제 투어 탐방 체험 봉사 봉사단 프로그램 사업 성과 보고 보고서 계약 입찰
    도서 서비스 시스템 정보 홈페이지 포털 신청서 서류 증명서 확인서 제출 문의 연락 상담 면접 채용 공채 인턴 성취
    인턴십 취업 창업 현장 현장실습 실습 연수 교환 유학 방문 초청 초대 개강 종강 개교 기념 기념식 정기 정기총회
    임시 긴급 필독 중요 재안내 추가모집 추가선발 정원 인원 정원외 장비 시설 공사 점검 정전 단수 통제 출입 주차
    방학 학기 학년 학년도 하계 동계 계절 계절학기 전기 후기 상반기 하반기 상시 수시모집 정시모집 고사 면제
    이상 이하 이내 미만 초과 기준 조건 자격 요건 우선 순위 순서 차수 배정 추첨 선착순 무료 유료 할인 지급
    지급일 환불 반환 납부 등록금 수강 수강신청 수강정정 정정기간 성적표 학점 학점교류 교류 협력 협약 공동
    주관 주최 후원 협찬 제공 배부 배포 게시 게재 공유 소개 홍보 소식 뉴스 보도 언론 기사 인터뷰 영상 사진
    전시 전시회 공연 음악회 상영 상영회 오리엔테이션 설명 가이드 매뉴얼 양식 서식 첨부 파일 자료 원본 사본
    고객님 선배님 선생님 교수님 조교님 동문님 부모님 학부모님 어머님 아버님 주인님 여러분
    정보통신 전자 전기 반도체 소프트웨어 컴퓨터 정보 융합 로봇 건축 화학 환경 경영 경제 행정 법학 국어 영어
    산업 심리 미디어 문화 스포츠 게임 금융 부동산 인문 사회 자연 공학 예술 체육 교양 기초 심화 전문 고급
    신청자 지원자 선정자 이수자 우수자 성적우수자 전공자 졸업자 졸업예정자 해당자 참여자 응시자 등록자 수강자
    편입자 전입자 복학자 휴학자 연구자 관리자 담당자 책임자 보호자 소지자 이용자 사용자 신고자 추천자 대표자
    추천 임용 모범 우리 여러 이번 연계 전담 신진 임원 도움 고교 원어민 한국어 고운 좋은 착한 멋진 새로운
    전공별 기간별 지역별 국가별 유형별 전학년 고학년 신학기 정규직 인턴직 모집중 진행중 공휴일 온라인 이공계
    마감일 연구동 정정본 연장됨 노원구 한국인 외국인
    안내문 공고문 발표문 결과표 일정표 시간표 합격증 수상작 수상팀 당선작 입상작 선정작 기념품 제막식 인증서
    최종본 공개용 심사본 제출본 인쇄본 연구진 국내외 공무원 임직원 장애인 노약자 고령자 어린이 이야기
    제출자 미제출자 방문자 유공자 지도자 기부자 봉사자 진행자 강연자 심사자 주관자 국가직 지방직 경찰직 소방직
    공기업 전문직 강의동 연구상 봉사상 모범상 노력상 창의상 기술상 작품상 발표상 신인상 감사상 장관상 공헌상
    기여상 협동상 도전상 혁신상 성실상 개근상 정근상 효행상 선행상 우정상 교육상 학술상 논문상 시장상 총장상
    이벤트 전문가 기준표 진행표 지원서 추천서 이력서 서약서 신분증 소식지 성희롱 성폭력 성교육 진로 현황
    예정자 예치금 지원금 장려금 성과금 기부금 연구비 전형료 박사 석사 과정 석박사 박사과정 석사과정
"""
_STOPWORDS: Final[frozenset[str]] = frozenset(_STOPWORD_TEXT.split())
# Before these (with a space) a name-like word is a person: "홍길동 학생", "김철수 교수님".
_ROLES: Final[tuple[str, ...]] = ("교수님", "교수", "선생님", "선생", "학생", "조교", "동문", "박사", "석사", "학우", "님", "씨")
_PARTICLES: Final[frozenset[str]] = frozenset(
    {"", "의", "이", "가", "은", "는", "을", "를", "께", "께서", "에게", "과", "와", "도", "들", "님", "등", "으로", "로", "에"})
# Longest first, so "에게" goes before "게" would
_TRAILING_PARTICLES: Final[tuple[str, ...]] = ("으로", "에게", "께서", "은", "는", "을", "를", "이", "가", "의", "에",
                                               "과", "와", "도", "로")
# Words that make a title a roster, a prize list or a profile; a prize ending in 상 counts too (_is_prize).
_ROSTER = re.compile(
    r"합격자|최종\s*합격|합격|명단|선발자|선발\s*결과|선정\s*결과|선정자|당선자|당선|수상자|입상자|장학생|대상자|"
    r"학위\s*논문|공개\s*발표|논문\s*심사|심사\s*결과|심사|수상|입상|입선|가작|우승자|우승|졸업생|인터뷰"
)
# Bare, these mostly head ordinary titles ("심사 기준표 안내", "졸업생 진로 현황 조사"): after them
# only a full name right after the word counts, and full names in brackets.
_WEAK_ROSTER: Final[frozenset[str]] = frozenset({"심사", "졸업생", "인터뷰", "우승"})
# Words that end in 상 but are no prize, as words or as the end of one ("평점 3.0 이상", "동영상",
# "역량향상", "음악감상", "옥상", "기상 악화", "항상"), and the 상 of "in terms of" (시스템상 오류,
# 절차상 하자, 인터넷상, 계약상, 안전상 이유).
_NOT_PRIZE_TEXT = """
    이상 현상 영상 일상 향상 인상 보상 정상 세상 예상 사상 환상 협상 형상 실상 증상 진상 비상 무상 유상 화상
    손상 책상 밥상 감상 연상 발상 구상 육상 빙상 외상 중상 경상 위상 조상 문상 회상 상상 지상 해상 선상 천상
    평상 계상 단상 부상 참상 도상 옥상 기상 최상 가상 명상 공상 망상 몽상 궁상 온상 항상 양상 의상 임상 통상
    템상 넷상 산상 차상 약상 률상 법상 식상 칙상 안상 전상 격상 간상 론상 례상 서상 류상 면상 목상 편상 건상
    업상 분상 강상 생상
"""
_NOT_PRIZE: Final[tuple[str, ...]] = tuple(_NOT_PRIZE_TEXT.split())
# Prizes that end like one of those: 우수논문상 (문상), 봉사상 and 도지사상 (사상), 신인상 (인상), 교육감상
# (감상), 연구상 (구상), 인기상 (기상), 우정상 (정상), 성실상 (실상), 창의상 (의상), 교육상 (육상), 도전상
# (전상), 주연상 (연상), 모범학생상 (생상), 학회상 (회상), 창업상 (업상), 도약상 (약상).
_PRIZE_TEXT = """
    논문상 봉사상 감사상 지사상 신인상 교육감상 연구상 인기상 연기상 우정상 열정상 성실상 창의상 교육상 도전상
    주연상 조연상 학생상 학회상 협회상 창업상 학업상 도약상
"""
_PRIZE_ENDINGS: Final[tuple[str, ...]] = tuple(_PRIZE_TEXT.split())
# Right before a bare 대상, these make it the grand prize: "경진대회 대상", "공모전 대상"
_CONTEST_ENDINGS: Final[tuple[str, ...]] = (
    "대회", "공모전", "경연", "경진", "해커톤", "챌린지", "콘테스트", "올림피아드", "페스티벌", "시상식")
# A bare 대상 is the grand prize before these: "대상 수상", "대상을 받아", "대상 차지"
_AWARD_VERBS: Final[tuple[str, ...]] = ("수상", "받", "차지", "영예")
# The person's department, school or year between a roster word and the name: "[수상] 전자공학과 4학년 홍길동"
_AFFILIATION_ENDINGS: Final[tuple[str, ...]] = ("과", "부", "대학", "대학원", "학년", "과정", "전공")
_GAP_BEFORE_YEAR = re.compile(r"[\s:：\-–—~·,/.!)\]】」>]*\d{1,4}")
# A pass or a prize right after a full name: "홍길동 합격", "김철수 수상".
_PASS_WORDS: Final[tuple[str, ...]] = ("수상", "합격", "당선", "입상", "우승", "준우승")
_OBITUARY = re.compile(r"부고|별세|소천|타계|영면|(?:부친|모친|빙부|빙모|시부|시모|조부|조모|장인|장모|본인|배우자)상")
_HANGUL_RUN = re.compile(r"[가-힣]+")
_BRACKETS = re.compile(r"\(([^()]*)\)|\[([^\[\]]*)\]|【([^【】]*)】|「([^「」]*)」|<([^<>]*)>")
_LIST_GAP = re.compile(r"\s*[,·/、ㆍ]\s*|\s+")
_LIST_MARKS = ",·/、ㆍ"
_COUNTED = re.compile(r"\s*(?:외|등)\s*\d+\s*(?:명|인)")
# An affiliation between a name and its role: "(전자공학과)", " [로봇학부]"
_AFFILIATION = re.compile(r"\s?(?:\([^()]{1,40}\)|\[[^\[\]]{1,40}\])")
# "홍길동 외" (but not 외국인, 외부)
_ALONE_OE = re.compile(r"\s+외(?![가-힣])")
_IN_BRACKETS_OE = re.compile(r"\s*외\s*[)\]】」>]")
# What may stand between a roster or prize word and the name after it: "공개발표 - 홍길동", "[수상] 홍길동"
_GAP_AFTER_ROSTER = re.compile(r"[\s:：\-–—~·,/.!)\]】」>]*")
_AFTER_ID = re.compile(r"\[REDACTED_(?:ACCOUNT|IDENTIFIER)\][\s(:]*$")
_BEFORE_ID = re.compile(r"^\s*[(:]?\s*\[REDACTED_(?:ACCOUNT|IDENTIFIER)\]")
_MASK = "*＊○◯●◎ㅇOＯo×X"
_PARTLY_HIDDEN = re.compile(
    rf"(?<![가-힣A-Za-z])(?:[{''.join(sorted(_SURNAMES - {'제'}))}])"
    rf"(?:[{_MASK}]{{1,3}}[가-힣]?|[가-힣][{_MASK}]{{1,2}})(?=[님씨\s,·/)\]]|$)"
)
# title words skipped while looking for the name after a roster word, at most this many (and as
# many affiliation words besides)
_SKIP_AFTER_ROSTER = 3


@dataclass(frozen=True, slots=True)
class RedactedHumanFields:
    """All bounded human-authored fields after the privacy pass."""

    title: str
    category_name: str
    department: str
    body: str


def _replace_account_run(match: re.Match[str]) -> str:
    """Mask an account-like run unless it is explicitly a notice number."""
    before = match.string[max(0, match.start() - 12) : match.start()]
    after = match.string[match.end() : match.end() + 2]
    if _NOTICE_NUMBER_BEFORE.search(before) and (not before.rstrip().endswith("제") or after.lstrip().startswith("호")):
        return match.group(0)
    return "[REDACTED_ACCOUNT]"


def _is_name(word: str) -> bool:
    """2-4 syllables, a surname first, not an office/thing ending, not a stopword."""
    if not 2 <= len(word) <= 4 or word in _STOPWORDS or word[-1] in _NOT_NAME_ENDINGS:
        return False
    if word[:2] in _COMPOUND_SURNAMES:
        return len(word) >= 3
    # four syllables are mostly two words: 추가서류, 제출안내
    return word[0] in _SURNAMES and not (len(word) == 4 and (word[:2] in _STOPWORDS or word[2:] in _STOPWORDS))


def _is_full_name(word: str) -> bool:
    """A name-like word as long as a whole name: three syllables, or a two-syllable surname and more.
    Two-syllable words (주말, 지역, 소감) are mostly ordinary words."""
    return _is_name(word) and (len(word) == 3 or word[:2] in _COMPOUND_SURNAMES)


def _base(word: str) -> str:
    """The word without one trailing particle: 대상을 → 대상, 수상자는 → 수상자."""
    for particle in _TRAILING_PARTICLES:
        if word.endswith(particle) and len(word) > len(particle) + 1:
            return word[: -len(particle)]
    return word


def _is_prize(word: str) -> bool:
    """대상, 금상, 최우수상, 총장상, 우수논문상 ...: a word ending in 상 that is a known prize or no
    common word."""
    return any(len(w) >= 2 and w.endswith("상") and (w.endswith(_PRIZE_ENDINGS) or not w.endswith(_NOT_PRIZE))
               for w in {word, _base(word)})


def _prize_runs(text: str, runs: list[re.Match[str]]) -> list[re.Match[str]]:
    """The prize words of a title. A bare 대상 is one only before 수상/받/차지/영예, right after a
    contest, or with another prize in the title; else it is "for" (재학생 대상 성교육)."""
    prizes = [k for k, run in enumerate(runs) if _is_prize(run.group(0))]
    others = any(_base(runs[k].group(0)) != "대상" for k in prizes)
    out = []
    for k in prizes:
        run = runs[k]
        if _base(run.group(0)) == "대상" and not others:
            before = runs[k - 1] if k else None
            after = re.match(r"\s*([가-힣]+)", text[run.end() :])
            if not ((after and after.group(1).startswith(_AWARD_VERBS))
                    or (before and not text[before.end() : run.start()].strip()
                        and _base(before.group(0)).endswith(_CONTEST_ENDINGS))):
                continue
        out.append(run)
    return out


def _is_roster_word(word: str) -> bool:
    return bool(_ROSTER.search(word)) or _is_prize(word)


def _award_follows(rest: str, *, spaced: bool = True) -> bool:
    """`rest` starts with a prize or a pass: " 최우수상", " 합격", " 대상 수상". A bare 대상 is mostly
    "for" (학생 대상), so it counts only before 수상/받/차지/영예."""
    match = re.match(r"(\s*)([가-힣]+)(?:\s+([가-힣]+))?", rest)
    if not match or (spaced and not match.group(1)):
        return False
    word, after = match.group(2), match.group(3) or ""
    if _base(word) == "대상":
        return after.startswith(_AWARD_VERBS)
    return _is_prize(word) or word.startswith(_PASS_WORDS)


def _role_follows(rest: str) -> bool:
    """`rest` (the text after a word) starts with a space and a role word, or with 외 N명. An
    affiliation in brackets may come first ("(전자공학과) 학생", gap or not), and after one a prize
    or a pass marks a person too ("(경영학부) 대상 수상")."""
    if _COUNTED.match(rest):
        return True
    affiliation = _AFFILIATION.match(rest)
    if affiliation:
        rest = rest[affiliation.end() :]
        if _COUNTED.match(rest) or _award_follows(rest, spaced=False):
            return True
    gap = len(rest) - len(rest.lstrip())
    if not gap and not affiliation:
        return False
    run = _HANGUL_RUN.match(rest, gap)
    if run is None:
        return False
    return any(run.group(0).startswith(role) and run.group(0)[len(role):] in _PARTICLES for role in _ROLES)


def _list_before(text: str, runs: list[re.Match[str]], i: int) -> list[int]:
    """Indexes of name-like runs listed right before run i ("홍길동, 김철수" before "이영희")."""
    out = []
    while i > 0 and _LIST_GAP.fullmatch(text[runs[i - 1].end() : runs[i].start()]) and _is_name(runs[i - 1].group(0)):
        i -= 1
        out.append(i)
    return out


def _name_runs(text: str, start: int, end: int, minimum: int, *, full: bool = False) -> set[tuple[int, int]]:
    """Spans of name-like runs (full names only if `full`) between start and end that come `minimum`
    or more in a row."""
    spans: set[tuple[int, int]] = set()
    streak: list[tuple[int, int]] = []
    looks_like = _is_full_name if full else _is_name
    for run in _HANGUL_RUN.finditer(text, start, end):
        name = looks_like(run.group(0))
        if not name or (streak and not _LIST_GAP.fullmatch(text[streak[-1][1] : run.start()])):
            if len(streak) >= minimum:
                spans.update(streak)
            streak = []
        if name:
            streak.append(run.span())
    if len(streak) >= minimum:
        spans.update(streak)
    return spans


def _roster_ends(text: str, runs: list[re.Match[str]]) -> tuple[list[int], list[int]]:
    """Where each roster or prize word ends (the end of the word it is part of: 최종합격자, 수상작), as
    (strong, weak): a bare 심사, 졸업생, 인터뷰 or 우승 is weak (_WEAK_ROSTER)."""
    strong, weak = set(), set()
    for match in _ROSTER.finditer(text):
        run = next((r for r in runs if r.start() < match.end() <= r.end()), None)
        (weak if match.group(0) in _WEAK_ROSTER else strong).add(run.end() if run else match.end())
    strong.update(run.end() for run in _prize_runs(text, runs))
    return sorted(strong), sorted(weak - strong)


def _is_affiliation(word: str) -> bool:
    """A department, school, year or role: 전자공학과, 로봇학부, 공과대학, 학년, 박사과정, 학생."""
    return word in _ROLES or (len(word) >= 2 and word.endswith(_AFFILIATION_ENDINGS))


def _names_right_after(text: str, runs: list[re.Match[str]], end: int) -> set[tuple[int, int]]:
    """A full name right after a roster or prize word that ends at `end`, past separators, closing
    brackets, other title words and the person's affiliation ("공개발표 안내 - 홍길동", "[수상] 홍길동",
    "[수상] 전자공학과 4학년 홍길동"), and the names listed right after that one ("대상 홍길동, 김산")."""
    spans: set[tuple[int, int]] = set()
    position, skipped, affiliations = end, 0, 0
    for k, run in enumerate(runs):
        if run.start() < end:
            continue
        word, gap = run.group(0), text[position : run.start()]
        if not (_GAP_AFTER_ROSTER.fullmatch(gap) or (word.startswith("학년") and _GAP_BEFORE_YEAR.fullmatch(gap))):
            return spans
        if _is_affiliation(word):  # not a name, and not one of the title words counted below
            affiliations += 1
            if affiliations > _SKIP_AFTER_ROSTER:
                return spans
            position = run.end()
            continue
        if word in _STOPWORDS or _is_roster_word(word):
            skipped += 1
            if skipped > _SKIP_AFTER_ROSTER:
                return spans
            position = run.end()
            continue
        if not _is_full_name(word):
            return spans
        spans.add(run.span())
        last = run
        for following in runs[k + 1 :]:
            gap = text[last.end() : following.start()]
            listed = bool(_LIST_GAP.fullmatch(gap)) and any(mark in gap for mark in _LIST_MARKS)
            if not ((listed and _is_name(following.group(0))) or (gap.isspace() and _is_full_name(following.group(0)))):
                break
            spans.add(following.span())
            last = following
        return spans
    return spans


def _person(text: str, run: re.Match[str]) -> bool:
    """A name-like word that its neighbours mark as a person (see the module docstring)."""
    word, rest = run.group(0), text[run.end() :]
    if not _is_name(word):
        return False
    if _role_follows(rest) or _AFTER_ID.search(text[: run.start()]) or _BEFORE_ID.match(rest):
        return True
    if text[: run.start()].endswith(("(", "[", "【", "「", "<")) and _IN_BRACKETS_OE.match(rest):
        return True  # "(김산 외)"
    return _is_full_name(word) and bool(_ALONE_OE.match(rest) or _award_follows(rest))


def redact_names(value: str) -> str:
    """Replace people's names with [REDACTED_NAME] (see the module docstring for the rules)."""
    text = _PARTLY_HIDDEN.sub(NAME, value)
    runs = list(_HANGUL_RUN.finditer(text))
    spans: set[tuple[int, int]] = set()
    for i, run in enumerate(runs):
        word = run.group(0)
        if word[-1] in "님씨" and _is_name(word[:-1]):  # 홍길동님, 김철수씨
            spans.add((run.start(), run.end() - 1))
            spans.update(runs[k].span() for k in _list_before(text, runs, i))
        elif _person(text, run):
            spans.add(run.span())
            spans.update(runs[k].span() for k in _list_before(text, runs, i))
    if _OBITUARY.search(text):
        spans.update(run.span() for run in runs if _is_name(run.group(0)))
    strong, weak = _roster_ends(text, runs)
    if strong:
        spans |= _name_runs(text, strong[0], len(text), 2)
    if strong or weak:
        for bracket in _BRACKETS.finditer(text):
            inner_start = bracket.start() + 1
            inner_end = bracket.end() - 1
            spans |= _name_runs(text, inner_start, inner_end, 2, full=not strong)
            inner = text[inner_start:inner_end].strip()
            # one word alone in brackets: a full name has three syllables, (성취) or (기간연장) do not
            if len(inner) == 3 and _HANGUL_RUN.fullmatch(inner) and _is_name(inner):
                spans.add((text.index(inner, inner_start), text.index(inner, inner_start) + len(inner)))
        for end in strong + weak:
            spans |= _names_right_after(text, runs, end)
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + NAME + text[end:]
    return text


def may_name_someone(text: str) -> bool:
    """A roster, prize or pass word (a bare 대상 meaning "for" aside) and a word that still looks like
    a full name. For titles that went through redact_text: what is left may be a name it missed."""
    runs = list(_HANGUL_RUN.finditer(text))
    roster = bool(_ROSTER.search(text)) or bool(_prize_runs(text, runs))
    return roster and any(_is_full_name(r.group(0)) and not _is_roster_word(r.group(0)) for r in runs)


def redact_text(value: str) -> str:
    """Redact e-mail, phone, resident-ID and identifier patterns, then names (v3)."""
    redacted = _LABELLED.sub(r"\g<label>\g<separator>[REDACTED_IDENTIFIER]", value)
    redacted = _RESIDENT.sub("[REDACTED_RESIDENT_ID]", redacted)
    redacted = _EMAIL.sub("[REDACTED_EMAIL]", redacted)
    redacted = _EMAIL_SPELLED.sub("[REDACTED_EMAIL]", redacted)
    redacted = _KOREAN_MOBILE.sub("[REDACTED_PHONE]", redacted)
    redacted = _KOREAN_LANDLINE.sub("[REDACTED_PHONE]", redacted)
    redacted = _KOREAN_SERVICE.sub("[REDACTED_PHONE]", redacted)
    redacted = _INTERNATIONAL_PHONE.sub("[REDACTED_PHONE]", redacted)
    redacted = _ACCOUNT_RUN.sub(_replace_account_run, redacted)
    redacted = _PARTIAL_ID.sub(lambda m: m.group(0) if m.group(0).isdigit() else "[REDACTED_IDENTIFIER]", redacted)
    return redact_names(redacted)


def _redact_and_cap(value: str, limit: int) -> str:
    return normalize_text(redact_text(value), limit)


def redact_human_fields(
    *, title: str, category_name: str, department: str, body: str
) -> RedactedHumanFields:
    """Redact and cap every persisted human-authored field."""
    return RedactedHumanFields(
        title=_redact_and_cap(title, 500),
        category_name=_redact_and_cap(category_name, 64),
        department=_redact_and_cap(department, 200),
        body=_redact_and_cap(body, 4000),
    )


# --- notice bodies -----------------------------------------------------------------------------
# A body names its contact person in ways a title rarely does: "문의: 학생복지팀 홍길동", "담당자 홍길동
# 주무관", "홍길동(02-940-5114)". redact_body masks those on top of redact_text, line by line. Titles
# keep redact_text alone, so REDACTION_LEVEL (norm_v on stored titles) does not move.
_CONTACT_LABEL = re.compile(r"(?:담당자|담당|문의처|문의|연락처|작성자|성명|이름)\s*[:：)\]】]")
# name-like words looked at after a contact label, on the same line
_AFTER_LABEL_WORDS = 3
_STAFF_TITLE = re.compile(
    r"\s*(?:주무관|팀장|과장|계장|차장|부장|실장|센터장|처장|원장|직원|담당자|담당|매니저|연구원|행정원)(?![가-힣])")
_NEXT_TO_CONTACT = re.compile(r"\s*\(?\s*\[REDACTED_(?:PHONE|EMAIL)\]")
_CONTACT_BEFORE = re.compile(r"\[REDACTED_(?:PHONE|EMAIL)\][)\]]?[\s,/]*$")


def _contact_names(line: str) -> set[tuple[int, int]]:
    runs = list(_HANGUL_RUN.finditer(line))
    spans: set[tuple[int, int]] = set()
    for label in _CONTACT_LABEL.finditer(line):
        after = [run for run in runs if run.start() >= label.end()][:_AFTER_LABEL_WORDS]
        spans.update(run.span() for run in after if _is_name(run.group(0)))
    for run in runs:
        if not _is_name(run.group(0)):
            continue
        rest, before = line[run.end():], line[: run.start()]
        if _STAFF_TITLE.match(rest) or _NEXT_TO_CONTACT.match(rest) or _CONTACT_BEFORE.search(before):
            spans.add(run.span())
    return spans


def redact_body(value: str) -> str:
    """redact_text for a notice body, plus contact people: a name-like word among the three words after
    a contact label (담당자:, 문의), before a staff title (주무관, 팀장) or next to a masked phone number
    or e-mail address. Line breaks are kept."""
    out = []
    for line in value.splitlines():
        text = redact_text(line)
        for start, end in sorted(_contact_names(text), reverse=True):
            text = text[:start] + NAME + text[end:]
        out.append(text)
    return "\n".join(out)


def looks_like_full_name(word: str) -> bool:
    """A Hangul word shaped like a whole person's name (three syllables, a common surname first)."""
    return _is_full_name(word)
