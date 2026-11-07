"""Turn a KLAS snapshot from the extension into tool results (same shapes as kw-mcp).

Snapshot (fields already whitelisted by the extension):
  {"semester": "2026,2", "fetched_at": "...",
   "courses": [{subj, yearhakgi, subjNm, profNm, lctrumSchdulInfo, subjLabel}],
   "course_posts": [{subjNm, typeNm, title, registDt}],
   "sections": {"<subj>": {"tasks": [...], "online": [...], "projects": [...], "quizzes": [...]}},
   "missing": {"lists": ["quizzes"], "courses": [{"kind": "tasks", "course": "<subjNm>"}]}}

"missing" (agents after 0.9.0) names the lists KLAS failed to give the PC agent: whole lists, and
lists that failed for single courses. It is absent when everything was read.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from kw_mcp.service import (  # shared with the PC agent: one definition
    KIND_NAMES,
    PATH_KIND,
    SNAPSHOT_KIND,
    _percent,  # KLAS progress as a whole percent: 100, "100" and "100.0" are all done
    is_lecture,
    parse_due,  # "23:59" is open through 23:59:59, as in kw-mcp
    period_time,
    schedule_note,
    schedule_slots,
)
from kw_mcp.text import clean, html_to_text
from mcp.server.mcpserver.exceptions import ToolError

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


def _t(value: Any) -> Any:
    """Text from a KLAS row as kw-mcp gives it (kw_mcp.text.clean: invisible format and control
    characters, a known way to hide instructions, are dropped); other values as they are."""
    return clean(value) if isinstance(value, str) else value


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", "", text or "").casefold()


def _matches(name: str | None, course: str | None) -> bool:
    """kw_mcp.service's rule: case- and space-insensitive substring; None matches everything."""
    return course is None or _norm(course) in _norm(name)


def _courses(snap: dict, course: str | None) -> list[dict]:
    """The snapshot's courses matching `course`. A filter that matches none is an error naming the
    courses there are (kw-mcp says the same), never an empty list the model takes as "nothing due"."""
    all_courses = snap.get("courses", [])
    found = [c for c in all_courses if _matches(c.get("subjNm"), course)]
    if course and not found:
        names = ", ".join(_t(c.get("subjNm")) or "?" for c in all_courses)
        raise ToolError(f"'{course}'와 일치하는 과목이 없습니다. 이 학기 과목: {names}")
    return found


# The snapshot's list names -> the kinds deadlines() reports (kw-mcp's names for both).
_KIND = {snap_kind: PATH_KIND[path] for path, snap_kind in SNAPSHOT_KIND.items()}
_LIST_NAME = {"tasks": "과제", "online": "온라인 강의", "projects": "팀 프로젝트", "quizzes": "퀴즈"}


def _missing(snap: dict) -> tuple[list[str], list[dict]]:
    """What KLAS didn't give the PC agent: whole lists by name, and {"kind", "course"} per course."""
    missing = snap.get("missing") or {}
    return ([k for k in missing.get("lists") or [] if isinstance(k, str)],
            [m for m in missing.get("courses") or [] if isinstance(m, dict)])


def _rows(snap: dict, key: str, course: str | None, partial: bool = False):
    """(course, row) for one list. A list KLAS didn't give the PC agent (for every course, or for a
    course `course` covers) is an error, as in kw-mcp's list tools, never an empty list read as
    "nothing due". deadlines() passes partial=True and names the gap in `incomplete` instead."""
    found = _courses(snap, course)
    if not partial:
        lists, courses = _missing(snap)
        names = {c.get("subjNm") for c in found}
        failed = [str(m.get("course")) for m in courses if m.get("kind") == key and m.get("course") in names]
        if key in lists or failed:
            where = f"{', '.join(_t(name) for name in failed)} 과목의 " if failed and key not in lists else ""
            raise ToolError(f"PC 에이전트가 KLAS에서 {where}{_LIST_NAME.get(key, key)} 목록을 읽지 못했습니다. "
                            "잠시 뒤 다시 시도해 주세요.")
    for c in found:
        for row in (snap.get("sections", {}).get(c.get("subj"), {}) or {}).get(key, []) or []:
            yield c, row


def courses(snap: dict) -> list[dict]:
    out = []
    for c in snap.get("courses", []):
        schedule = _t(c.get("lctrumSchdulInfo"))
        row = {"course": _t(c.get("subjNm")), "professor": _t(c.get("profNm")), "schedule": schedule,
               "slots": schedule_slots(schedule), "semester": c.get("yearhakgi")}
        if note := schedule_note(schedule):
            row["note"] = note
        out.append(row)
    return out


def assignments(snap: dict, course: str | None = None, pending_only: bool = False,
                partial: bool = False) -> list[dict]:
    out = []
    for c, t in _rows(snap, "tasks", course, partial):
        submitted = t.get("submityn") == "Y"
        if pending_only and submitted:
            continue
        out.append({"course": _t(c.get("subjNm")), "title": clean(t.get("title")), "opens": fmt_kst(t.get("startdate")),
                    "due": fmt_kst(t.get("expiredate")), "late_due": fmt_kst(t.get("reexpiredate")),
                    "submitted": submitted, "score": t.get("score")})
    return sorted(out, key=lambda a: a["due"] or "9999")


def online_lectures(snap: dict, course: str | None = None, incomplete_only: bool = False,
                    partial: bool = False) -> list[dict]:
    out = []
    for c, o in _rows(snap, "online", course, partial):
        if not is_lecture(o):
            continue
        progress = _percent(o.get("prog"))
        if incomplete_only and progress is not None and progress >= 100:
            continue
        out.append({"course": _t(c.get("subjNm")), "week": o.get("weekNo") or o.get("weeklyseq"),
                    "module": _t(o.get("moduletitle")), "title": _t(o.get("sbjt")), "opens": fmt_kst(o.get("startDate")),
                    "due": fmt_kst(o.get("endDate")), "progress_percent": progress,
                    "required_time": o.get("rcognTime"), "achieved_time": o.get("achivTime")})
    return sorted(out, key=lambda a: a["due"] or "9999")


def projects(snap: dict, course: str | None = None, partial: bool = False) -> list[dict]:
    out = [{"course": _t(c.get("subjNm")), "title": _t(p.get("title")), "purpose": _t(p.get("purpose")),
            "opens": fmt_kst(p.get("startdate")), "due": fmt_kst(p.get("expiredate")),
            "late_due": fmt_kst(p.get("reexpiredate")), "submitted": p.get("submityn") == "Y",
            "score": p.get("score"), "team_no": p.get("teamno")} for c, p in _rows(snap, "projects", course, partial)]
    return sorted(out, key=lambda a: a["due"] or "9999")


def quizzes(snap: dict, course: str | None = None, pending_only: bool = False,
            partial: bool = False) -> list[dict]:
    out = []
    for c, q in _rows(snap, "quizzes", course, partial):
        submitted = q.get("issubmit") == "Y"
        if pending_only and submitted:
            continue
        out.append({"course": _t(c.get("subjNm")), "title": _t(q.get("papernm")), "type": _t(q.get("examtypenm")),
                    "opens": fmt_kst(q.get("sdt")), "due": fmt_kst(q.get("edt")), "submitted": submitted,
                    "time_limit_minutes": _int(q.get("examtime")), "questions": q.get("examcnt"),
                    "total_score": q.get("totalscore")})
    return sorted(out, key=lambda a: a["due"] or "9999")


def course_posts(snap: dict, course: str | None = None) -> list[dict]:
    _courses(snap, course)  # an unknown course is an error; a known one with no posts is just []
    return [{"course": _t(n.get("subjNm")), "board": _t(n.get("typeNm")), "title": html_to_text(n.get("title")),
             "posted": fmt_kst(n.get("registDt"))}
            for n in snap.get("course_posts", []) if _matches(n.get("subjNm"), course)]


def deadlines(snap: dict, now: datetime, days: int = 7) -> dict:
    """kw_mcp.service.deadlines on a snapshot: what's due from now to the end (23:59:59) of the day
    `days` days from today. Lists the PC agent couldn't read are named in `incomplete`."""
    days = max(0, min(days, 180))
    # KLAS deadlines cluster at 23:59:59: the window ends at the end of the target day.
    until = (now + timedelta(days=days)).replace(hour=23, minute=59, second=59, microsecond=0)
    every = {"assignment": assignments(snap, partial=True), "online_lecture": online_lectures(snap, partial=True),
             "quiz": quizzes(snap, partial=True), "team_project": projects(snap, partial=True)}
    open_rows = {
        "assignment": [a for a in every["assignment"] if not a["submitted"]],
        "online_lecture": [o for o in every["online_lecture"]
                           if o["progress_percent"] is None or o["progress_percent"] < 100],
        "quiz": [q for q in every["quiz"] if not q["submitted"]],
        "team_project": [p for p in every["team_project"] if not p["submitted"]],
    }
    items = []
    for kind, rows in open_rows.items():
        for row in rows:
            due, late_due = parse_due(row["due"]), parse_due(row.get("late_due"))
            if due and now <= due <= until:
                items.append({"kind": kind, **row})
            elif due and late_due and due < now <= late_due:  # assignments and team projects have late windows
                items.append({"kind": kind, "late_submission": True, **row})
    items.sort(key=lambda i: i["late_due"] if i.get("late_submission") else i["due"])
    out = {"semester": snap.get("semester"), "now": now.strftime("%Y-%m-%d %H:%M"),
           "until": until.strftime("%Y-%m-%d %H:%M"), "days": days, "data_as_of": snap.get("fetched_at"),
           "items": items}
    lists, courses = _missing(snap)
    # kw_mcp.service.KlasService.deadlines' rule, on the lists it reads (pending assignments and quizzes,
    # unfinished lectures, every team project): rows, but not one due or late due that parses, means
    # KLAS renamed the field or changed its date format, and "nothing due" would be wrong.
    judged = {**open_rows, "team_project": every["team_project"]}
    unreadable = [kind for kind, rows in judged.items()
                  if rows and not any(parse_due(r["due"]) or parse_due(r.get("late_due")) for r in rows)]
    # kw-mcp's shape: whole kinds in missing, single courses in missing_courses
    misses = [{"kind": _KIND.get(m.get("kind"), m.get("kind")), "course": m.get("course")} for m in courses]
    missing = [_KIND.get(k, k) for k in lists]
    missing += [k for k in unreadable if k not in missing]
    if missing or misses:
        message = "KLAS에서 일부 목록을 읽지 못해 빠진 마감이 있을 수 있습니다."
        if unreadable:
            message += (f" {'·'.join(KIND_NAMES[k] for k in unreadable)} 목록은 마감 시각을 읽지 못했습니다"
                        "(KLAS 형식이 바뀐 것 같습니다). KLAS에서 직접 확인하세요.")
        out["incomplete"] = {"missing": missing, **({"missing_courses": misses} if misses else {}),
                             **({"unreadable_due": unreadable} if unreadable else {}), "message": message}
    return out


def classes_on(snap: dict, now: datetime) -> list[dict]:
    weekday = WEEKDAYS[now.weekday()]
    out = []
    for c in courses(snap):
        for slot in c["slots"]:
            if slot["weekday"] == weekday:
                out.append({"course": c["course"], "periods": slot["periods"], "time": period_time(slot["periods"]),
                            "room": slot["room"], "professor": c["professor"]})
    return sorted(out, key=lambda x: x["periods"][0] if x["periods"] else 99)
