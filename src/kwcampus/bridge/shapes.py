"""Turn a KLAS snapshot from the extension into tool results (same shapes as kw-mcp).

Snapshot (fields already whitelisted by the extension):
  {"semester": "2026,2", "fetched_at": "...",
   "courses": [{subj, yearhakgi, subjNm, profNm, lctrumSchdulInfo, subjLabel}],
   "course_posts": [{subjNm, typeNm, title, registDt}],
   "sections": {"<subj>": {"tasks": [...], "online": [...], "projects": [...], "quizzes": [...]}}}
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from kw_mcp.service import CLASS_CHANGE_WORDS as CLASS_CHANGE_WORDS  # re-exported for app.py
from kw_mcp.service import is_lecture, period_time, schedule_note  # shared with the PC agent: one definition

from kwcampus.config import KST

WEEKDAYS = "월화수목금토일"

_SLOT = re.compile(r"^\s*([월화수목금토일])\s*([\d,\s]+)교시\s*(?:/\s*(.*?))?\s*$")


def parse_kst(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=KST)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(KST) if parsed.tzinfo else parsed.replace(tzinfo=KST)


def fmt_kst(value: str | None) -> str | None:
    parsed = parse_kst(value)
    return parsed.strftime("%Y-%m-%d %H:%M") if parsed else value


def parse_schedule(text: str | None) -> list[dict[str, Any]]:
    slots = []
    for part in re.split(r",\s*(?=[월화수목금토일]\s)", text or ""):
        m = _SLOT.match(part)
        if m:
            slots.append({"weekday": m.group(1), "periods": [int(x) for x in re.findall(r"\d+", m.group(2))],
                          "room": (m.group(3) or "").strip()})
    return slots


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _matches(name: str | None, course: str | None) -> bool:
    return course is None or course.casefold() in (name or "").casefold()


def _rows(snap: dict, key: str, course: str | None):
    for c in snap.get("courses", []):
        if _matches(c.get("subjNm"), course):
            for row in (snap.get("sections", {}).get(c.get("subj"), {}) or {}).get(key, []) or []:
                yield c, row


def courses(snap: dict) -> list[dict]:
    out = []
    for c in snap.get("courses", []):
        schedule = c.get("lctrumSchdulInfo")
        row = {"course": c.get("subjNm"), "professor": c.get("profNm"), "schedule": schedule,
               "slots": parse_schedule(schedule), "semester": c.get("yearhakgi")}
        if note := schedule_note(schedule):
            row["note"] = note
        out.append(row)
    return out


def assignments(snap: dict, course: str | None = None, pending_only: bool = False) -> list[dict]:
    out = []
    for c, t in _rows(snap, "tasks", course):
        submitted = t.get("submityn") == "Y"
        if pending_only and submitted:
            continue
        out.append({"course": c.get("subjNm"), "title": t.get("title"), "opens": fmt_kst(t.get("startdate")),
                    "due": fmt_kst(t.get("expiredate")), "late_due": fmt_kst(t.get("reexpiredate")),
                    "submitted": submitted, "score": t.get("score")})
    return sorted(out, key=lambda a: a["due"] or "9999")


def online_lectures(snap: dict, course: str | None = None, incomplete_only: bool = False) -> list[dict]:
    out = []
    for c, o in _rows(snap, "online", course):
        if not is_lecture(o):
            continue
        progress = _int(o.get("prog"))
        if incomplete_only and progress is not None and progress >= 100:
            continue
        out.append({"course": c.get("subjNm"), "week": o.get("weekNo") or o.get("weeklyseq"),
                    "module": o.get("moduletitle"), "title": o.get("sbjt"), "opens": fmt_kst(o.get("startDate")),
                    "due": fmt_kst(o.get("endDate")), "progress_percent": progress,
                    "required_time": o.get("rcognTime"), "achieved_time": o.get("achivTime")})
    return sorted(out, key=lambda a: a["due"] or "9999")


def projects(snap: dict, course: str | None = None) -> list[dict]:
    out = [{"course": c.get("subjNm"), "title": p.get("title"), "purpose": p.get("purpose"),
            "opens": fmt_kst(p.get("startdate")), "due": fmt_kst(p.get("expiredate")),
            "late_due": fmt_kst(p.get("reexpiredate")), "submitted": p.get("submityn") == "Y",
            "score": p.get("score"), "team_no": p.get("teamno")} for c, p in _rows(snap, "projects", course)]
    return sorted(out, key=lambda a: a["due"] or "9999")


def quizzes(snap: dict, course: str | None = None, pending_only: bool = False) -> list[dict]:
    out = []
    for c, q in _rows(snap, "quizzes", course):
        submitted = q.get("issubmit") == "Y"
        if pending_only and submitted:
            continue
        out.append({"course": c.get("subjNm"), "title": q.get("papernm"), "type": q.get("examtypenm"),
                    "opens": fmt_kst(q.get("sdt")), "due": fmt_kst(q.get("edt")), "submitted": submitted,
                    "time_limit_minutes": _int(q.get("examtime")), "questions": q.get("examcnt"),
                    "total_score": q.get("totalscore")})
    return sorted(out, key=lambda a: a["due"] or "9999")


def course_posts(snap: dict, course: str | None = None) -> list[dict]:
    return [{"course": n.get("subjNm"), "board": n.get("typeNm"), "title": n.get("title"),
             "posted": fmt_kst(n.get("registDt"))}
            for n in snap.get("course_posts", []) if _matches(n.get("subjNm"), course)]


def deadlines(snap: dict, now: datetime, days: int = 7) -> dict:
    days = max(0, min(days, 180))
    until = now + timedelta(days=days)
    items = []
    for a in assignments(snap, pending_only=True):
        due, late_due = parse_kst(a["due"]), parse_kst(a["late_due"])
        if due and now <= due <= until:
            items.append({"kind": "assignment", **a})
        elif due and late_due and due < now <= late_due:
            items.append({"kind": "assignment", "late_submission": True, **a})
    for kind, rows in (("online_lecture", online_lectures(snap, incomplete_only=True)),
                       ("quiz", quizzes(snap, pending_only=True)),
                       ("team_project", [p for p in projects(snap) if not p["submitted"]])):
        for row in rows:
            due, late_due = parse_kst(row["due"]), parse_kst(row.get("late_due"))
            if due and now <= due <= until:
                items.append({"kind": kind, **row})
            elif due and late_due and due < now <= late_due:  # team projects have late windows too
                items.append({"kind": kind, "late_submission": True, **row})
    items.sort(key=lambda i: i["late_due"] if i.get("late_submission") else i["due"])
    return {"semester": snap.get("semester"), "now": now.strftime("%Y-%m-%d %H:%M"), "days": days,
            "data_as_of": snap.get("fetched_at"), "items": items}


def classes_on(snap: dict, now: datetime) -> list[dict]:
    weekday = WEEKDAYS[now.weekday()]
    out = []
    for c in courses(snap):
        for slot in c["slots"]:
            if slot["weekday"] == weekday:
                out.append({"course": c["course"], "periods": slot["periods"], "time": period_time(slot["periods"]),
                            "room": slot["room"], "professor": c["professor"]})
    return sorted(out, key=lambda x: x["periods"][0] if x["periods"] else 99)
