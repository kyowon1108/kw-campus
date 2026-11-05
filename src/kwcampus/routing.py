"""Which Discord channel a main-board category goes to."""

from __future__ import annotations

# channel keys match the keys of the JSON in the /kw/discord/channels parameter
CHANNEL_KEYS = ("notice", "academic", "scholarship", "career", "international")

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
}

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
}


def channel_for(category_id: str) -> str:
    return CATEGORY_TO_CHANNEL.get(category_id, "notice")
