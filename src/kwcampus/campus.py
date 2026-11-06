"""Lambda: campus data that changes rarely — the cafeteria week menu and the academic calendar.

Runs once on weekday mornings (before the notice collection window). It fetches the
cafeteria page until this week's menu is stored (a week counts only with at least one meal on
it, so last week's page fetched early on Monday is tried again the next morning), and the
calendar only when the stored copy is older than a week, so most days make 0-1 requests and a
run never makes more than two. A week is stored under the Monday of the dates it shows.

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
from kwcampus.config import COLLECTOR_ENABLED_PARAM, KST, PUBLIC_TABLE, WWW_DAILY_CAP
from kwcampus.fetch import WORST_CASE_S, fetch_list_page
from kwcampus.kwnotice.source import NOTICE_HOST
from kwcampus.obs import log, metrics

CAFETERIA_URL = "https://www.kw.ac.kr/ko/life/facility11.jsp"
CALENDAR_URL = "https://www.kw.ac.kr/KWBoard/list5_detail.jsp"
CALENDAR_MAX_AGE = timedelta(days=7)
DAILY_CAP = WWW_DAILY_CAP  # shared origin budget with the notice collector
MAX_REQUESTS = 2  # per run

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


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _has_meals(item: dict | None) -> bool:
    return bool(item) and any(d.get("meals") for d in item.get("days") or [])


class _Www:
    """This run's www requests: at most MAX_REQUESTS, 61 s apart, and none once the gate says no."""

    def __init__(self, table, clock, sleep, time_left):
        self.table, self.clock, self.sleep, self.time_left = table, clock, sleep, time_left
        self.sent_at: datetime | None = None
        self.sent, self.stopped = 0, None

    def fetch(self, url: str, form: dict | None = None) -> tuple[str | None, str]:
        if self.stopped:
            return None, self.stopped
        if self.sent >= MAX_REQUESTS:
            return None, "next_run"
        wait = 0.0
        if self.sent_at is not None:  # really wait; never two requests in a row
            wait = max(0.0, (self.sent_at + politeness.MIN_INTERVAL - self.clock()).total_seconds() + 1)
        if self.time_left is not None and self.time_left() < wait + WORST_CASE_S:
            return None, "no_time"
        if wait:
            self.sleep(wait)
        permit = politeness.acquire(self.table, NOTICE_HOST, self.clock(), daily_cap=DAILY_CAP)
        if not permit.granted:
            if permit.reason in ("cooldown", "disabled_after_blocks", "daily_cap"):
                self.stopped = permit.reason  # nothing else this run either, and no sleeping for it
            return None, permit.reason
        self.sent_at, self.sent = self.clock(), self.sent + 1
        result = fetch_list_page(url, form)
        if result.block_reason:
            if politeness.is_server_signal(result.block_reason):
                politeness.record_block(self.table, NOTICE_HOST, self.clock(), result.block_reason, result.retry_after)
                metrics(Blocked=int(result.block_reason in politeness.HARD),
                        Transient=int(result.block_reason not in politeness.HARD))
            log("campus_blocked", url=url.rsplit("/", 1)[-1], reason=result.block_reason, status=result.status)
            return None, result.block_reason
        politeness.record_success(self.table, NOTICE_HOST)
        return result.html, "ok"


def _calendar_due(item: dict | None, now: datetime) -> bool:
    return not item or now.timestamp() - int(item.get("fetched_at", 0)) > CALENDAR_MAX_AGE.total_seconds()


def run(now: datetime, table, clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        time_left: Callable[[], float] | None = None) -> dict[str, object]:
    clock = clock or (lambda: datetime.now(UTC))
    if not params.is_true(COLLECTOR_ENABLED_PARAM):
        return {"status": "disabled"}
    today = now.astimezone(KST).date()
    monday = _monday(today)
    out: dict[str, object] = {"status": "ok"}
    www = _Www(table, clock, sleep, time_left)

    have_week = table.get_item(Key={"PK": "CAMPUS#cafeteria", "SK": f"W#{monday}"}).get("Item")
    if not _has_meals(have_week) and politeness.robots_allows(CAFETERIA_URL):
        html, reason = www.fetch(CAFETERIA_URL)
        days = parse_cafeteria(html) if html else []
        week = _monday(date.fromisoformat(days[0]["date"])) if days else None
        if days and week is not None and week >= monday - timedelta(days=7):
            table.put_item(Item={"PK": "CAMPUS#cafeteria", "SK": f"W#{week}", "days": days,
                                 "fetched_at": int(now.timestamp()),
                                 "ttl": int((now + timedelta(days=60)).timestamp())})
        if not days:
            out["cafeteria"] = reason if reason != "ok" else "no_table"
        elif week != monday:  # last week's page still up: tried again tomorrow at 07:30
            out["cafeteria"] = "stale_week"
        elif not any(d["meals"] for d in days):
            out["cafeteria"] = "no_meals"
        else:
            out["cafeteria"] = f"{len(days)} days"

    # In December, also keep next year's calendar so "this week"/"next month" can see January. The
    # two refreshes drift apart: next year's waits when this year's was read in the same run.
    years = [today.year] + ([today.year + 1] if today.month == 12 else [])
    for year in years:
        if not politeness.robots_allows(CALENDAR_URL):
            break
        stored = table.get_item(Key={"PK": "CAMPUS#calendar", "SK": f"Y#{year}"}).get("Item")
        if not _calendar_due(stored, now):
            continue
        key = "calendar" if year == today.year else "calendar_next_year"
        if year != today.year and "calendar" in out:
            out[key] = "next_run"
            continue
        html, reason = www.fetch(CALENDAR_URL, {"sy": str(year), "sm": ""})
        events = parse_calendar(html, year) if html else []
        if events:
            table.put_item(Item={"PK": "CAMPUS#calendar", "SK": f"Y#{year}", "events": events,
                                 "fetched_at": int(now.timestamp())})
        out[key] = f"{len(events)} events" if events else reason if reason != "ok" else "no_events"

    log("campus_ok", requests=www.sent, **out)
    metrics(CampusFetched=www.sent)
    return out


def handler(event, context):
    time_left = (lambda: context.get_remaining_time_in_millis() / 1000) if context else None
    return run(datetime.now(UTC), boto3.resource("dynamodb").Table(PUBLIC_TABLE), time_left=time_left)


# --- read side for the MCP tools -------------------------------------------------

def menu_for(table, day: date) -> dict | None:
    monday = _monday(day)
    item = table.get_item(Key={"PK": "CAMPUS#cafeteria", "SK": f"W#{monday}"}).get("Item")
    if not item:
        return None
    return next((d for d in item["days"] if d["date"] == day.isoformat()), None)


def calendar_events(table, year: int) -> list[dict]:
    item = table.get_item(Key={"PK": "CAMPUS#calendar", "SK": f"Y#{year}"}).get("Item")
    return list(item["events"]) if item else []
