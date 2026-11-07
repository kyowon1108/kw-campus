"""Lambda: D-day reminders under announced notices, weekdays 08:30 KST.

A notice whose page was read (extract.py) and that has a 신청마감 ahead gets a reply under its own
Discord message at D-3 and again at D-1: "⏰ 신청 마감 D-3 · 11/18(수) 24:00까지". The job runs on weekdays
only, so a level goes out on the last run on or before its day, never after it: a Tuesday deadline's
D-3 (a Saturday) goes out on Friday as "D-4". Two levels due in one run (a Monday deadline, on
Friday) make one reply.

This Lambda only finds what is due and puts "remind" jobs on the Discord queue. The sender
(discord.py) posts them under the same rate limiting as every other job, after checking that the
notice is still up, its deadline unchanged and its message still there, and records each level in
the delivery table, so a notice gets each level at most once. Replies mention nobody. Removed
notices, seed rows (never announced) and the FAQ are skipped here already. While the
/kw/collector/notify switch is off nothing is sent.
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime

import boto3
from boto3.dynamodb.conditions import Attr, Key

from kwcampus import params
from kwcampus.config import KST, NOTIFY_ENABLED_PARAM, PUBLIC_TABLE
from kwcampus.dispatcher import send_jobs
from kwcampus.extract import next_application, parse_at
from kwcampus.obs import log, metrics
from kwcampus.public_data import SOURCES
from kwcampus.routing import channel_for

LEVELS = (3, 1)  # D-3 and D-1


def gap_to_next_run(today: date) -> int:
    """Days from this run to the next weekday run (Friday: 3)."""
    return {4: 3, 5: 2}.get(today.weekday(), 1)


def due_levels(days_left: int, gap: int) -> list[int]:
    """Levels whose day is today or falls before the next run."""
    return [level for level in LEVELS if days_left - gap < level <= days_left]


def jobs_for(items: list[dict], today: date, now_ts: float | None = None) -> list[dict]:
    now_ts = time.time() if now_ts is None else now_ts
    gap, jobs = gap_to_next_run(today), []
    for item in items:
        if item.get("removed") or item.get("seed") or (item.get("ttl") and int(item["ttl"]) <= now_ts):
            continue
        channel = channel_for(str(item.get("category_id") or ""))
        deadline = next_application(item.get("extracted"), today)
        if channel is None or deadline is None:
            continue
        levels = due_levels((parse_at(deadline["at"])[0] - today).days, gap)
        if levels:
            jobs.append({"action": "remind", "key": {"PK": item["PK"], "SK": item["SK"]}, "channel": channel,
                         "levels": levels, "at": deadline["at"]})
    return jobs


def _items(table) -> list[dict]:
    """Notices with extracted facts, every source."""
    items = []
    for source in SOURCES:
        kwargs = {"KeyConditionExpression": Key("PK").eq(f"SRC#{source}") & Key("SK").begins_with("N#"),
                  "FilterExpression": Attr("extracted").exists()}
        while True:
            page = table.query(**kwargs)
            items.extend(page["Items"])
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return items


def handler(event, context):
    if not params.is_true(NOTIFY_ENABLED_PARAM):
        log("remind_skip", reason="notify_off")
        return {"status": "notify_off"}
    table = boto3.resource("dynamodb").Table(PUBLIC_TABLE)
    items = _items(table)
    jobs = jobs_for(items, datetime.now(UTC).astimezone(KST).date())
    if jobs:
        sqs = boto3.client("sqs")
        for start in range(0, len(jobs), 10):
            send_jobs(sqs, jobs[start : start + 10])
    log("remind_ok", notices=len(items), jobs=len(jobs))
    metrics(RemindJobs=len(jobs))
    return {"status": "ok", "jobs": len(jobs)}
