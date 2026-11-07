"""What the public MCP and JSON API serve: categories, FAQ, expiry, masking, cross-posts, sources, order."""

import time
from datetime import date

import pytest

from kwcampus import public_data

TODAY = "2026-11-05"


def put(table, source, post_id, title, category, posted=TODAY, **extra):
    table.put_item(Item={"PK": f"SRC#{source}", "SK": f"N#{post_id}", "source": source, "post_id": str(post_id),
                         "title": title, "category_id": category, "department": "교육지원팀", "posted_date": posted,
                         "updated_date": posted, "pinned": False, "seed": False, "content_hash": "x",
                         "url": f"https://example.kw.ac.kr/{post_id}", **extra})


@pytest.fixture
def table(aws):
    public_data.clear_cache()
    yield aws["public"]
    public_data.clear_cache()


def ids(rows):
    return [r["post_id"] for r in rows]


def test_unknown_category_is_an_error_not_everything(table):
    put(table, "kw-notice", 1, "도서관 휴관 안내", "general")
    with pytest.raises(public_data.UnknownCategory) as caught:
        public_data.latest(table, "급식")
    assert caught.value.as_dict()["error"] == "unknown_category" and "학사" in caught.value.as_dict()["valid"]
    with pytest.raises(public_data.UnknownCategory):
        public_data.search(table, "휴관", "없는분류")


def test_aliases_and_parts_of_labels():
    r = public_data.resolve_category
    assert r("장학금") == r("장학") == r("등록/장학") == {"registration-scholarship"}
    assert r("취업관련") == r("채용") == r("취업") == {"job"}
    assert r("건축") == {"dept-archi", "dept-architecture"}
    assert r("학과") == public_data.DEPARTMENT_IDS and "dept-aicon" in r("학과") and "dept-ee" in r("학과")
    assert r("학사공지") == {"academic"} and r(None) is None and r("  ") is None
    assert public_data.category_id("전정대") == "dept-ei" and public_data.category_id("건축") is None


def test_faq_is_left_out_of_latest_unless_asked(table):
    put(table, "kw-notice", 1, "수강신청 안내", "academic")
    put(table, "kw-faq", 2, "수강신청 FAQ", "faq")
    assert ids(public_data.latest(table)) == ["1"]
    assert ids(public_data.search(table, "")) == ["1"]  # the bridge's "new notices" ask with no words
    assert ids(public_data.latest(table, "FAQ")) == ["kw-faq-2"]
    assert set(ids(public_data.search(table, "수강신청"))) == {"1", "kw-faq-2"}


def test_expired_and_removed_items_are_not_served(table):
    put(table, "kw-notice", 1, "지난 공지", "general", ttl=int(time.time()) - 60)
    put(table, "kw-notice", 2, "내린 공지", "general", removed=True)
    put(table, "kw-notice", 3, "살아 있는 공지", "general", ttl=int(time.time()) + 86400)
    assert ids(public_data.latest(table)) == ["3"] and public_data.get(table, "1") is None


def test_titles_are_masked_again_on_the_way_out(table):
    put(table, "dept-ee", 9, "[축하] 전자공학과 홍길동 학생 수상 (010.1234.5678)", "dept-ee")  # stored before v2
    row = public_data.get(table, "dept-ee-9")
    assert row["title"] == "[축하] 전자공학과 [REDACTED_NAME] 학생 수상 ([REDACTED_PHONE])"
    assert public_data.search(table, "홍길동") == []
    assert set(row) == {"post_id", "title", "category", "category_id", "department", "posted_date",
                        "updated_date", "pinned", "url"}


def test_cross_posts_fold_into_one(table):
    title = "[대학혁신지원사업] 2026 광운대학교 취업박람회 개최 안내"
    put(table, "kw-job", 53606, title, "job", "2026-11-04", first_seen=3)
    put(table, "dept-ee", 646, title, "dept-ee", "2026-11-04", first_seen=2)
    put(table, "dept-law", 519, "[대학혁신지원사업] 2026 광운대학교 취업박람회 개최 안내!", "dept-law", "2026-11-03")
    put(table, "kw-notice", 1, "도서관 휴관 안내", "general", "2026-11-02")
    rows = public_data.latest(table, None, 10)
    assert ids(rows) == ["kw-job-53606", "1"] and rows[0]["also"] == ["dept-ee-646", "dept-law-519"]
    assert [b["category"] for b in rows[0]["also_boards"]] == ["전자공학과", "법학부"]  # each copy's board
    assert ids(public_data.latest(table, "학과", 10)) == ["dept-ee-646"]
    assert ids(public_data.latest(table, "법학부", 10)) == ["dept-law-519"]  # each department still finds its own


def test_sources_filter_applies_before_the_limit(table):
    for n in range(40):  # a busy day on the department boards
        put(table, "dept-ee", 700 + n, f"전자공학과 공지 {n}", "dept-ee", first_seen=100 + n)
    put(table, "kw-notice", 55001, "11/7 새빛관 정전 안내", "facilities", "2026-11-03", first_seen=5)
    rows = public_data.search(table, "", None, date(2026, 11, 2), 5, sources=("kw-notice",))
    assert ids(rows) == ["55001"]
    assert ids(public_data.latest(table, None, 1, sources=["kw-notice"])) == ["55001"]
    assert len(public_data.latest(table, None, 50)) == 41  # no sources: as before


def test_same_day_order_is_first_seen_then_number(table):
    put(table, "dept-ee", 900, "학과 공지", "dept-ee", first_seen=10)
    put(table, "kw-notice", 55002, "메인 공지 나중", "general", first_seen=20)
    put(table, "kw-notice", 55001, "메인 공지 먼저", "general", first_seen=20)
    put(table, "aicon", 3517, "위젯 공지", "dept-aicon", "2026-11-04", first_seen=30)
    assert ids(public_data.latest(table, None, 10)) == ["55002", "55001", "dept-ee-900", "aicon-3517"]


def test_a_department_without_a_board_reads_its_colleges_board():
    r, d = public_data.resolve_category, public_data.departments_of
    assert r("정보융합학부") == r("정보 융합 학부") == r("정보융합학부 공지") == {"dept-aicon"}
    assert d("정보융합학부") == d("정보 융합 학부") == d("인공지능융합대학 정보융합학부") == {"정보융합학부"}
    assert r("국제통상학부") == r("경영학부") == {"dept-biz"} and d("경영학부") == {"경영학부"}
    # a department with its own board, a college, a word that names no department: no department filter
    assert r("전자공학과") == {"dept-ee"} and r("자율전공학부") == {"dept-liberal"}
    for value in ("전자공학과", "자율전공학부", "인공지능융합대학", "학과", None):
        assert d(value) == set(), value
    # two departments of one college: both filter the college board (not the whole board)
    assert r("정보융합학부 로봇학부") == {"dept-aicon"} and d("정보융합학부 로봇학부") == {"정보융합학부", "로봇학부"}
    with pytest.raises(public_data.UnknownCategory):
        r("없는학부")
    with pytest.raises(public_data.UnknownCategory):  # two colleges' boards: refused, never mixed
        r("정보융합학부 경영학부")


def test_a_departments_view_of_its_college_board(table):
    put(table, "aicon", 3524, "졸업작품 발표회 안내", "dept-aicon", "2026-11-04", department="정보융합학부")
    put(table, "aicon", 3523, "졸업작품 발표회 안내", "dept-aicon", "2026-11-04", department="소프트웨어학부")
    put(table, "aicon", 3519, "졸업작품 발표회 안내", "dept-aicon", "2026-11-03", department="인공지능융합대학")
    put(table, "aicon", 3400, "졸업작품 발표회 안내", "dept-aicon", "2026-09-01", department="인공지능융합대학")
    put(table, "aicon", 3518, "[REDACTED_NAME] 교수 특강", "dept-aicon", "2026-11-03", department="정보융합학부")
    put(table, "aicon", 3517, "[REDACTED_NAME] 교수 특강", "dept-aicon", "2026-11-03", department="")
    put(table, "dept-biz", 2551, "취업박람회", "dept-biz", "2026-11-04", department="경영대학")
    rows = public_data.latest(table, "정보융합학부", 10)
    # the college-wide copy of the same week gives way to the department's; an older one is another notice,
    # and masked titles never count as the same
    assert ids(rows) == ["aicon-3524", "aicon-3518", "aicon-3517", "aicon-3400"]
    assert ids(public_data.search(table, "발표회", "정보융합학부")) == ["aicon-3524", "aicon-3400"]
    assert ids(public_data.latest(table, "국제통상학부", 10)) == ["dept-biz-2551"]  # 경영대학's posts are college-wide
    assert len(public_data.latest(table, "인공지능융합대학", 10)) == 6  # the college itself: every copy


def test_two_departments_without_boards_keep_only_their_rows(table):
    """"정보융합학부 로봇학부": both departments' rows and the college-wide ones, never the whole college board
    (other schools' posts) as when only a single department was filtered."""
    for no, school in ((3524, "정보융합학부"), (3526, "로봇학부"), (3523, "소프트웨어학부"), (3522, "컴퓨터정보공학부")):
        put(table, "aicon", no, "졸업작품 발표회 안내", "dept-aicon", "2026-11-04", department=school)
    put(table, "aicon", 3519, "졸업작품 발표회 안내", "dept-aicon", "2026-11-03", department="인공지능융합대학")
    put(table, "aicon", 3510, "학생회 선거 안내", "dept-aicon", "2026-11-02", department="인공지능융합대학")
    put(table, "aicon", 3509, "소프트웨어학부 캡스톤 안내", "dept-aicon", "2026-11-02", department="소프트웨어학부")
    rows = public_data.latest(table, "정보융합학부 로봇학부", 10)
    assert ids(rows) == ["aicon-3526", "aicon-3524", "aicon-3510"]  # each department's copy, not the college-wide one
    assert {r["department"] for r in rows} == {"정보융합학부", "로봇학부", "인공지능융합대학"}
    assert ids(public_data.search(table, "캡스톤", "정보융합학부 로봇학부")) == []


def facts(**extra):
    base = {"deadlines": [{"kind": "신청마감", "at": "2026-11-18T23:59:59+09:00", "text": "11.18.(수) 24:00까지"}],
            "target": "재학생", "eligibility": ["직전학기 12학점 이상"], "how_to_apply": "KLAS > 장학 > 장학금신청",
            "amount": "200만원", "contact_office": "학생복지팀 (02-940-5114)", "category_hint": "장학",
            "summary": "재학생 대상 2학기 장학생 선발", "sources_used": ["body"]}
    return {**base, **extra}


def test_search_finds_the_read_facts_after_the_titles(table):
    put(table, "kw-notice", 1, "2026학년도 2학기 교내장학 안내", "registration-scholarship", "2026-11-05",
        extracted=facts())
    put(table, "kw-notice", 2, "프론티어장학금 신청 안내", "registration-scholarship", "2026-11-01")  # never read
    put(table, "kw-notice", 3, "성적우수 장학 안내", "registration-scholarship", "2026-11-06",
        extracted=facts(summary="프론티어장학금과 함께 받을 수 없음", eligibility=["직전학기 15학점 이상"]))
    assert ids(public_data.search(table, "12학점")) == ["1"]  # only in the eligibility
    assert ids(public_data.search(table, "장학 klas 200만원")) == ["3", "1"]  # every word, across title and facts
    assert ids(public_data.search(table, "장학 없는말")) == []
    # a title match comes first, then a newer notice that has the word only in its facts
    assert ids(public_data.search(table, "프론티어장학금")) == ["2", "3"]
    assert ids(public_data.search(table, "")) == ["3", "1", "2"]  # no words: newest first, as before


def test_search_never_reaches_what_the_facts_mask(table):
    # stored before the current masking rules: the name is masked on the way out, so never found
    put(table, "kw-notice", 1, "근로장학생 모집", "registration-scholarship",
        extracted=facts(how_to_apply="홍길동 조교에게 이메일 hong@kw.ac.kr로 제출"))
    row = public_data.get(table, "1")
    assert "홍길동" not in row["extracted"]["how_to_apply"] and "hong@" not in row["extracted"]["how_to_apply"]
    assert public_data.search(table, "홍길동") == [] and public_data.search(table, "hong@kw.ac.kr") == []
    assert public_data.search(table, "5114") == []  # the contact line is not searched
    assert ids(public_data.search(table, "조교에게 제출")) == ["1"]  # what is shown is searched
