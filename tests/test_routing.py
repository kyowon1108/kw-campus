from kwcampus import deptboards
from kwcampus.routing import CATEGORY_TO_CHANNEL, TOPIC_BY_KEY, TOPICS, topic_for


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
    assert {topic_for(b.category_id) for b in deptboards.BOARDS} == {"ei"}


def test_topic_keys_and_role_names_are_unique():
    assert len({t.key for t in TOPICS}) == len({t.role for t in TOPICS}) == len(TOPICS)
    assert "list" not in TOPIC_BY_KEY  # reserved for the "내 알림 보기" button
