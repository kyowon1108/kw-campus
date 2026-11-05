"""Lambda: campus data that changes rarely — the cafeteria week menu and the academic calendar.

Runs once on weekday mornings (before the notice collection window). It fetches the
cafeteria page only when this week's menu is missing or it is Monday, and the calendar
only when the stored copy is older than a week, so most days make 0-1 requests.

Items in kw-public:
  PK "CAMPUS#cafeteria"  SK "W#<monday>"   {days: [{date, weekday, meals: [{name, price, time, menu}]}]}
  PK "CAMPUS#calendar"   SK "Y#<year>"     {events: [{start, end, title}], fetched_at}
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import boto3
from bs4 import BeautifulSoup

from kwcampus import params, politeness
from kwcampus.config import COLLECTOR_ENABLED_PARAM, KST, PUBLIC_TABLE
from kwcampus.fetch import fetch_list_page
from kwcampus.kwnotice.source import NOTICE_HOST
from kwcampus.obs import log, metrics

CAFETERIA_URL = "https://www.kw.ac.kr/ko/life/facility11.jsp"
CALENDAR_URL = "https://www.kw.ac.kr/KWBoard/list5_detail.jsp"
CALENDAR_MAX_AGE = timedelta(days=7)
DAILY_CAP = 40  # shared origin budget with the notice collector

_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_MD = re.compile(r"(\d{1,2})\.(\d{1,2})")


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip() if node else ""


def parse_cafeteria(html: str) -> list[dict]:
    """[{date, weekday, meals: [{name, price, time, menu: [..]}]}] for the week shown."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.tbl-list")
    if table is None:
        return []
    days = []
    for th in table.select("thead th")[1:]:
        m = _DATE.search(_text(th))
        if m:
            days.append({"date": m.group(0), "weekday": _text(th.select_one(".nowDay")), "meals": []})
    for row in table.select("tbody tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        head = cells[0]
        meal = {
            "name": _text(head.select_one(".dietTitle")).replace("광운대 ", ""),
            "price": _text(head.select_one(".dietPrice")).strip("()"),
            "time": _text(head.select_one(".dietTime")),
        }
        for day, cell in zip(days, cells[1:], strict=False):
            pre = cell.find("pre")
            items = [x for x in (pre.get_text("\n") if pre else _text(cell)).split() if x]
            if items:
                day["meals"].append({**meal, "menu": items})
    return days


def _range(text: str, year: int) -> tuple[str, str] | None:
    parts = _MD.findall(text)
    if not parts:
        return None
    start = date(year, int(parts[0][0]), int(parts[0][1]))
    end = start
    if len(parts) > 1:
        end_month, end_day = int(parts[1][0]), int(parts[1][1])
        end = date(year + (1 if end_month < start.month else 0), end_month, end_day)
    return start.isoformat(), end.isoformat()


def parse_calendar(html: str, year: int) -> list[dict]:
    """Events of the whole year from the 'schedule-this-yearlist' block."""
    soup = BeautifulSoup(html, "html.parser")
    box = soup.select_one(".schedule-this-yearlist") or soup
    events, seen = [], set()
    for li in box.select(".list li"):
        when = _range(_text(li.find("strong")), year)
        title = _text(li.find("p"))
        if when and title and (when, title) not in seen:
            seen.add((when, title))
            events.append({"start": when[0], "end": when[1], "title": title})
    events.sort(key=lambda e: (e["start"], e["end"]))
    return events


def _fetch(table, now: datetime, url: str, form: dict | None = None):
    permit = politeness.acquire(table, NOTICE_HOST, now, daily_cap=DAILY_CAP)
    if not permit.granted:
        return None, permit.reason
    result = fetch_list_page(url, form)
    if result.block_reason:
        politeness.record_block(table, NOTICE_HOST, now, result.block_reason)
        return None, result.block_reason
    politeness.record_success(table, NOTICE_HOST)
    return result.html, "ok"


def run(now: datetime, table, clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep) -> dict[str, object]:
    clock = clock or (lambda: datetime.now(UTC))
    if not params.is_true(COLLECTOR_ENABLED_PARAM):
        return {"status": "disabled"}
    today = now.astimezone(KST).date()
    monday = today - timedelta(days=today.weekday())
    out: dict[str, object] = {"status": "ok"}

    have_week = table.get_item(Key={"PK": "CAMPUS#cafeteria", "SK": f"W#{monday}"}).get("Item")
    if not have_week or today.weekday() == 0:
        html, reason = _fetch(table, now, CAFETERIA_URL)
        days = parse_cafeteria(html) if html else []
        if days:
            table.put_item(Item={"PK": "CAMPUS#cafeteria", "SK": f"W#{monday}", "days": days,
                                 "fetched_at": int(now.timestamp()),
                                 "ttl": int((now + timedelta(days=60)).timestamp())})
        out["cafeteria"] = f"{len(days)} days" if days else reason if reason != "ok" else "no_table"

    stored = table.get_item(Key={"PK": "CAMPUS#calendar", "SK": f"Y#{today.year}"}).get("Item")
    if not stored or now.timestamp() - int(stored.get("fetched_at", 0)) > CALENDAR_MAX_AGE.total_seconds():
        if "cafeteria" in out:
            sleep(politeness.MIN_INTERVAL.total_seconds() + 1)  # really wait; never two requests in a row
        html, reason = _fetch(table, clock(), CALENDAR_URL, {"sy": str(today.year), "sm": ""})
        events = parse_calendar(html, today.year) if html else []
        if events:
            table.put_item(Item={"PK": "CAMPUS#calendar", "SK": f"Y#{today.year}", "events": events,
                                 "fetched_at": int(now.timestamp())})
        out["calendar"] = f"{len(events)} events" if events else reason if reason != "ok" else "no_events"

    # In December, also keep next year's calendar so "this week"/"next month" can see January.
    next_year = today.year + 1
    upcoming = table.get_item(Key={"PK": "CAMPUS#calendar", "SK": f"Y#{next_year}"}).get("Item")
    if today.month == 12 and (not upcoming or now.timestamp() - int(upcoming.get("fetched_at", 0))
                              > CALENDAR_MAX_AGE.total_seconds()):
        if "cafeteria" in out or "calendar" in out:
            sleep(politeness.MIN_INTERVAL.total_seconds() + 1)
        html, reason = _fetch(table, clock(), CALENDAR_URL, {"sy": str(next_year), "sm": ""})
        events = parse_calendar(html, next_year) if html else []
        if events:
            table.put_item(Item={"PK": "CAMPUS#calendar", "SK": f"Y#{next_year}", "events": events,
                                 "fetched_at": int(now.timestamp())})
        out["calendar_next_year"] = f"{len(events)} events" if events else reason if reason != "ok" else "no_events"

    log("campus_ok", **out)
    metrics(CampusFetched=int("cafeteria" in out) + int("calendar" in out))
    return out


def handler(event, context):  # noqa: ARG001
    return run(datetime.now(UTC), boto3.resource("dynamodb").Table(PUBLIC_TABLE))


# --- read side for the MCP tools -------------------------------------------------

def menu_for(table, day: date) -> dict | None:
    monday = day - timedelta(days=day.weekday())
    item = table.get_item(Key={"PK": "CAMPUS#cafeteria", "SK": f"W#{monday}"}).get("Item")
    if not item:
        return None
    return next((d for d in item["days"] if d["date"] == day.isoformat()), None)


def calendar_events(table, year: int) -> list[dict]:
    item = table.get_item(Key={"PK": "CAMPUS#calendar", "SK": f"Y#{year}"}).get("Item")
    return list(item["events"]) if item else []
