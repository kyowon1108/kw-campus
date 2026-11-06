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
    put(table, "dept-law", 519, "[학부공지] 2026 광운대학교 취업박람회 개최 안내", "dept-law", "2026-11-03")
    put(table, "kw-notice", 1, "도서관 휴관 안내", "general", "2026-11-02")
    rows = public_data.latest(table, None, 10)
    assert ids(rows) == ["kw-job-53606", "1"] and rows[0]["also"] == ["dept-ee-646", "dept-law-519"]
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
