"""The bridge's school_guide adds related notices read here (public_http.search_notices), like the
extension's: open deadlines first, else the last 90 days, "unavailable" on any failure. Made-up rows."""

import asyncio
from datetime import timedelta

from kwcampus import public_http
from kwcampus.bridge import app as bridge


def _row(post_id: str, title: str, posted: str, deadline: str | None = None, target: str = "") -> dict:
    out = {"post_id": post_id, "title": title, "posted_date": posted, "url": f"https://www.kw.ac.kr/{post_id}"}
    if deadline:
        out["deadline"] = {"kind": "신청마감", "at": deadline, "text": "마감"}
        out["extracted"] = {"target": target, "how_to_apply": "KLAS에서 신청"}
    return out


def test_open_notices_first_then_the_recent_ones(monkeypatch):
    today = bridge._now().date()
    soon, past = (today + timedelta(days=4)).isoformat(), (today - timedelta(days=2)).isoformat()
    calls = []

    def search(query, category=None, since=None, limit=20, open_only=False, deadline_after=None):
        calls.append((query, category, since, open_only))
        return [_row("1", "국가장학금 신청", past, f"{soon} 18:00", "재학생")] if open_only else []

    monkeypatch.setattr(public_http, "search_notices", search)
    out = asyncio.run(bridge.school_guide("국장 언제야?", category="장학"))
    assert out["answers"] and out["recent_notices"] == [
        {"title": "국가장학금 신청", "posted": past, "url": "https://www.kw.ac.kr/1", "deadline": f"{soon} 18:00",
         "target": "재학생", "how_to_apply": "KLAS에서 신청"}]
    assert out["recent_notices_note"].startswith("최근 공지가 있으면")
    # one search per kw-mcp notice_links.json query (장학: the category, and the 총동문회 board)
    assert calls == [("", "장학", None, True), ("총동문회", None, None, True)]

    def recent_only(query, category=None, since=None, limit=20, open_only=False, deadline_after=None):
        calls.append((query, category, since, open_only))
        return [] if open_only else [_row(query, f"{query} 안내", past)]

    calls.clear()
    monkeypatch.setattr(public_http, "search_notices", recent_only)
    out = asyncio.run(bridge.school_guide("휴학 어떻게 해?", category="학적변동"))
    assert {n["title"] for n in out["recent_notices"]} == {"휴학 안내", "복학 안내"}
    assert {c[2] for c in calls if not c[3]} == {(today - timedelta(days=90)).isoformat()}


def test_a_failure_or_no_link_never_sinks_the_answer(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("dynamodb down")

    monkeypatch.setattr(public_http, "search_notices", broken)
    out = asyncio.run(bridge.school_guide("국장 언제야?", category="장학"))
    assert out["answers"] and out["recent_notices"] == "unavailable"
    out = asyncio.run(bridge.school_guide("도서관 몇 시까지 해?", category="도서관"))  # no notices linked to this category
    assert out["answers"] and "recent_notices" not in out
