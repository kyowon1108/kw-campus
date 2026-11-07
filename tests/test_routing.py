from kwcampus import deptboards, routing, wixfeeds
from kwcampus.routing import (
    CATEGORY_TO_CHANNEL,
    TOPIC_BY_KEY,
    TOPICS,
    is_urgent,
    name_risk,
    topic_for,
    urgent_source,
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
    categories = ["dept-aicon", *(b.category_id for b in deptboards.BOARDS if not b.office),
                  *(f.category_id for f in wixfeeds.FEEDS)]
    missing = [c for c in categories if not (topic_for(c) and TOPIC_BY_KEY[topic_for(c)].college)]
    assert missing == [], "add these to routing.COLLEGE_OF"
    # and no college role is left without a board
    assert {t.key for t in TOPICS if t.college} == {topic_for(c) for c in categories}


def test_office_boards_go_to_their_channels_never_to_a_college():
    offices = {b.category_id for b in deptboards.BOARDS if b.office}
    assert offices == routing.OFFICE_CATEGORIES and offices <= set(CATEGORY_TO_CHANNEL)
    assert {c: topic_for(c) for c in sorted(offices)} == {
        "office-career": "career",  # 경력개발팀
        "office-grad": "academic",  # 일반대학원
        "office-humanrights": "general",  # 인권센터
        "office-huss": "academic",  # HUSS 사업단
        "office-inno": "general",  # 대학혁신사업단
        "office-kwiie": "general",  # 한국어교육센터
        "office-oia": "international",  # 국제처 유학생
        "office-semi": "academic",  # 반도체특성화대학사업단
        "office-startup": "general",  # 창업지원단
    }
    assert not any(urgent_source(c) for c in offices)  # 긴급 stays the main board's and the job board's
    assert topic_for("dept-electricjob") == "ei" and topic_for("dept-liberal") == "ingenium"
    assert is_urgent("[긴급] 교육 장소 변경 안내") and not urgent_source("office-huss")


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


def test_the_review_probe_titles():
    noisy = ["[긴급] 근로장학생 추가 모집", "[긴급] 학부연구생 모집", "긴급 안내: 국가장학금 2차 신청 마감 임박",
             "휴강 및 보강 계획서 제출 안내(교원용)", "추석 연휴 휴강", "개교기념일 휴강", "감염병 통제 지침",
             "출입통제시스템 교체 공사", "KLAS 서비스 중단(정기점검 11/15 02:00)", "도서관 전산 장애인 지원", "정보통제 보안교육"]
    real = ["수업 비대면 전환", "중간고사 고사실 변경", "시험 장소 변경", "화재로 새빛관 폐쇄", "셔틀버스 운행 중단",
            "KLAS 접속 불가", "Wi-Fi 장애", "결강"]
    assert [t for t in noisy if is_urgent(t)] == []
    assert [t for t in real if not is_urgent(t)] == []


def test_urgent_is_for_school_wide_sources_only():
    assert urgent_source("general") and urgent_source("academic") and urgent_source("job")
    assert not urgent_source("dept-ee") and not urgent_source("dept-media") and not urgent_source("dept-aicon")
    assert not urgent_source("faq")


def test_titles_that_may_carry_a_name():
    assert name_risk("general", "장학생 홍*동 외 3명") and name_risk("dept-ee", "이○○ 동문 특강")
    assert name_risk("dept-biz", "2026 경영경진대회 수상자 명단") and name_risk("dept-media", "故 교수 부고")
    assert not name_risk("general", "2026학년도 국가장학금 선발 결과 안내")  # school-wide titles carry no names
    assert not name_risk("dept-ee", "[필독*] 수강신청 안내") and not name_risk("dept-ee", "제○회 학술제")
