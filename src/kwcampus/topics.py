"""Subscription topics: one Discord role per topic, toggled by a button in #알림-설정.

Kept free of other imports so the button Lambda starts fast (Discord wants an answer within 3 s).
Which category belongs to which topic is routing.topic_for.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Topic:
    """A subscription: one Discord role that a button in #알림-설정 toggles and new notices mention."""

    key: str  # button custom_id suffix and key in /kw/discord/roles
    label: str  # button label
    role: str  # Discord role name (scripts/discord_setup.py creates it)


URGENT = "urgent"  # by title keywords (routing.is_urgent), any board; pinged even at night
TOPICS: tuple[Topic, ...] = (
    Topic(URGENT, "긴급 (휴강·통제·장애)", "긴급 알림"),
    Topic("academic", "학사", "학사 알림"),
    Topic("scholarship", "장학", "장학 알림"),
    Topic("career", "취업", "취업 알림"),
    Topic("international", "국제", "국제 알림"),
    Topic("general", "일반 (봉사·학생 등)", "일반 공지 알림"),
    Topic("aicon", "인공지능융합대학", "인공지능융합대학 알림"),
    Topic("ei", "전자정보공과대학", "전자정보공과대학 알림"),
)
TOPIC_BY_KEY: dict[str, Topic] = {t.key: t for t in TOPICS}
