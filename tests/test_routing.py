from kwcampus import deptboards, wixfeeds
from kwcampus.routing import (
    CATEGORY_TO_CHANNEL,
    TOPIC_BY_KEY,
    TOPICS,
    is_urgent,
    topic_for,
)


def test_every_announced_category_has_a_topic():
    for category in CATEGORY_TO_CHANNEL:
        assert topic_for(category) in TOPIC_BY_KEY, category
    assert topic_for("faq") is None  # never announced, so nothing to subscribe to
    assert topic_for("something-new") == "general"  # unknown categories go to 학교-공지


def test_topics_follow_channels_and_colleges():
    assert topic_for("academic") == "academic"
    assert topic_for("registration-scholarship") == "scholarship"
    assert topic_for("job") == topic_for("external") == "career"
    assert topic_for("international-student") == "international"
    assert {topic_for(c) for c in ("general", "student", "volunteer", "military")} == {"general"}
    assert topic_for("dept-aicon") == "aicon"
    assert {topic_for(f"dept-{k}") for k in ("ei", "ee", "semicon")} == {"ei"}
    assert topic_for("dept-biz") == "biz" and topic_for("dept-media") == topic_for("dept-korean") == "chss"
    assert topic_for("dept-architecture") == topic_for("dept-chemng") == "eng"
    assert topic_for("dept-kwgame") == "kwlife" and topic_for("dept-kwpa") == "law"


def test_every_department_board_and_feed_belongs_to_a_college():
    categories = ["dept-aicon", *(b.category_id for b in deptboards.BOARDS), *(f.category_id for f in wixfeeds.FEEDS)]
    missing = [c for c in categories if not (topic_for(c) and TOPIC_BY_KEY[topic_for(c)].college)]
    assert missing == [], "add these to routing.COLLEGE_OF"
    # and no college role is left without a board
    assert {t.key for t in TOPICS if t.college} == {topic_for(c) for c in categories}


def test_topic_keys_and_role_names_are_unique():
    assert len({t.key for t in TOPICS}) == len({t.role for t in TOPICS}) == len(TOPICS)
    assert "list" not in TOPIC_BY_KEY  # reserved for the "내 알림 보기" button


def test_urgent_titles_and_their_look_alikes():
    urgent = [
        "[긴급] 11/7(금) 수업 운영 안내", "긴급 공지: 셔틀버스 운행 중단", "2026-2학기 11월 7일 휴강 안내",
        "태풍으로 인한 휴교 안내", "비마관 정전 안내(11/8)", "새빛관 단수 예정 안내", "전자회로 강의실 변경 안내", "기초디지털실험 월요일반 강의요일 변경안내", "수업 장소 변경",
        "화도관 출입 통제 안내", "정문 차량 출입통제", "교내 도로 통제", "도서관 출입 제한 안내",
        "KLAS 시스템 장애 안내", "학사 서비스 중단 안내", "전산 장애 복구 안내",
    ]
    calm = [
        "출입국관리사무소 방문 일정 안내", "2026 장애학생 도우미 모집", "장애인식개선 교육 안내",
        "내부통제 점검 결과 공개", "긴급생활비 지원 장학금 신청", "단수전공 신청 안내", "정전기 방지 안내",
        "2026-2학기 수강신청 안내", "2026학년도 2학기 학위과정(프로그램) 최종변경 시행 안내",
        "출석부•학생증 사진 변경 방법 안내", "제4회 발달장애인 볼링대회 자원봉사자 모집",
    ]
    assert [t for t in urgent if not is_urgent(t)] == []
    assert [t for t in calm if is_urgent(t)] == []
