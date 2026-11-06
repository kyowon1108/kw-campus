"""Which Discord channel a category goes to, the label users see for it, and its subscription topic."""

from __future__ import annotations

from kwcampus import deptboards, wwwboards
from kwcampus.topics import TOPIC_BY_KEY, TOPICS, Topic

__all__ = ["CATEGORY_LABEL", "CATEGORY_TO_CHANNEL", "TOPICS", "TOPIC_BY_KEY", "Topic", "channel_for", "topic_for"]

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
    **{b.category_id: "department" for b in deptboards.BOARDS},
    "job": "career",  # 취업게시판
}
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
    **{b.category_id: b.name for b in deptboards.BOARDS},
    **{b.category_id: b.name for b in wwwboards.BOARDS},
}


def channel_for(category_id: str) -> str | None:
    """None: the category is never announced."""
    if category_id in QUIET_CATEGORIES:
        return None
    return CATEGORY_TO_CHANNEL.get(category_id, "notice")

# Colleges get their own topic. Every board in deptboards.BOARDS belongs to 전자정보공과대학;
# a board of another college needs a new Topic here and an entry below.
_COLLEGE_TOPIC: dict[str, str] = {
    "dept-aicon": "aicon",
    **{b.category_id: "ei" for b in deptboards.BOARDS},
}
# The other topics are the channels themselves; the 학교-공지 channel is the "general" topic.
_CHANNEL_TOPIC = {"notice": "general", "academic": "academic", "scholarship": "scholarship",
                  "career": "career", "international": "international"}


def topic_for(category_id: str) -> str | None:
    """None: the category is never announced, so nobody can subscribe to it."""
    channel = channel_for(category_id)
    if channel is None:
        return None
    return _COLLEGE_TOPIC.get(category_id) or _CHANNEL_TOPIC.get(channel)
