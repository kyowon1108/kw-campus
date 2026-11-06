"""Which Discord channel a category goes to, and the label users see for it."""

from __future__ import annotations

from kwcampus import deptboards, wixfeeds, wwwboards

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
    **{b.category_id: "department" for b in (*deptboards.BOARDS, *wixfeeds.FEEDS)},
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
    **{b.category_id: b.name for b in (*deptboards.BOARDS, *wixfeeds.FEEDS)},
    **{b.category_id: b.name for b in wwwboards.BOARDS},
}


def channel_for(category_id: str) -> str | None:
    """None: the category is never announced."""
    if category_id in QUIET_CATEGORIES:
        return None
    return CATEGORY_TO_CHANNEL.get(category_id, "notice")
