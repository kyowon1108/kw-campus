"""Lambda: one short message to the private #운영 channel, weekdays 08:40 KST.

It covers the previous KST day (on Monday: Friday to Sunday) and holds counts and the names of
boards, servers and alarms only: no notice titles, no Discord members, no invite hashes.

- boards: servers (politeness ORIGIN# state) and sources (SRC#<source> STATE) switched off or in a
  cool-down / back-off right now, sources with nothing new for a while (the newest first_seen of their
  notices, seed rows left out: 3 days for the main board, 7 for the 취업게시판, 60 for 총학생회, 90 for
  총동문회, 21 for the others; the FAQ isn't watched),
  and how many notices were new in the period;
- notice pages read (extract.py): done / failed / skipped for the day's cap, from the KwCampus metrics
  ExtractOk, ExtractFail + ExtractRefused and ExtractSkippedCap;
- reading health: of the period's new notices, how many were read in full (complete, body_only,
  privacy_skip), in part (partial, failed), are still pending (no read record, or a retry planned) or had
  only a title to read; the page queue (BODYQ) and its oldest row's age; notices first seen 14 days
  before the period that were never read (their queue row ran out): any of those makes the day worth a look;
  and how many older notices the sweep still has to read again (sweep.remaining: "+" while some source hasn't
  been walked yet).
  Also sent as Digest* metrics;
- Discord delivery: jobs waiting on kw-discord, jobs in kw-discord-dlq, the sender's retries;
- invites: the DINVITES#<date> counters of the period and the running total (DINVITES#total);
- CloudWatch alarms named kw-* in ALARM.

When nothing needs a look it is one line: "✅ 어제 이상 없음 · 새 공지 12 · 초대 3". A part that couldn't be
read says so ("확인 못 함") and counts as something to look at.

The message goes to the Discord queue as an "ops" job; the sender posts it to the "ops" channel of
/kw/discord/channels (scripts/discord_setup.py sets it). This function holds no Discord secret.
Reads: kw-public GetItem on ORIGIN#* and SRC#* (state rows) and MARK#* (when read records began, the sweep's
state), Query on
SRC#* for first_seen, seed, removed, extract_v and the read record only (QUERY_ATTRIBUTES), Query on BODYQ for
queued_at only (both always projected), kw-bridge GetItem on DINVITES#* only,
GetQueueAttributes on the two Discord queues, cloudwatch:GetMetricData and DescribeAlarms (kw-*).
"""

from __future__ import annotations

import os
from collections import Counter
from datetime import UTC, datetime, timedelta

import boto3
from boto3.dynamodb.conditions import Key

from kwcampus import (
    deptboards,
    extract,
    invites,
    politeness,
    public_data,
    smallboards,
    sweep,
    wixfeeds,
)
from kwcampus.config import DISCORD_QUEUE_URL, KST, NOTICE_SOURCE, PUBLIC_TABLE
from kwcampus.dispatcher import send_jobs
from kwcampus.kwnotice.source import NOTICE_HOST
from kwcampus.obs import NAMESPACE, log, metrics

# 총동문회 posts about once a month and 총학생회 in bursts (none from 09-09 to 11-07, 2026): longer before they count
STALE_DAYS = {NOTICE_SOURCE: 3, "kw-job": 7, smallboards.ALUMNI.source: 90, smallboards.COUNCIL.source: 60}
DEFAULT_STALE_DAYS = 21
NOT_WATCHED = frozenset({"kw-faq"})  # the FAQ changes rarely and is never announced
BACKLOG_WARN = 20  # jobs still waiting on kw-discord at 08:40 (nothing is collected overnight)
MAX_NAMES = 5
EXTRACT_METRICS = ("ExtractOk", "ExtractFail", "ExtractRefused", "ExtractSkippedCap", "DiscordRetries")
UNREAD = "확인 못 함"
# all the notices' Query may return (infra grants just these); `read` is the read record (counts, no text),
# extract_v marks a notice read before read records existed
QUERY_ATTRIBUTES = ("first_seen", "seed", "removed", "read", "extract_v")
QUEUE_ATTRIBUTES = ("queued_at",)  # the page queue (BODYQ): when each row was queued, nothing else
# A queue row lives this long (store.BODY_QUEUE_TTL): a notice first seen that long before the period and
# still unread was never read.
QUEUE_DAYS = 14
DONE = frozenset({"complete", "body_only", "privacy_skip"})
# Read records began with the first one written (extract.mark_reads leaves the time in kw-public): a notice
# first seen before that was queued under the old rules (일반, the FAQ and the Wix feeds never were), so only
# later ones count as expired unread; none do before the mark exists.


def period(now: datetime) -> tuple[datetime, datetime, str, list[str]]:
    """(start, end, label, KST dates) of the days this morning's message covers."""
    today = now.astimezone(KST).date()
    end = datetime(today.year, today.month, today.day, tzinfo=KST)
    days = 3 if today.weekday() == 0 else 1
    start = end - timedelta(days=days)
    dates = [(start + timedelta(days=i)).date().isoformat() for i in range(days)]
    return start, end, "금~일" if days == 3 else "어제", dates


def origins() -> list[str]:
    """Every server the collectors' gate knows (the 인공지능융합대학 widget shares E3)."""
    return sorted({NOTICE_HOST, *(b.server for b in deptboards.BOARDS), *(f.host for f in wixfeeds.FEEDS),
                   *(b.server for b in smallboards.BOARDS), smallboards.THUMB_HOST})


def _bucket(item: dict, now: float) -> str:
    """done, partial (final, short of something: partial or failed, or a retry that never came), pending (not
    read yet, or a retry still planned) or title (nothing to read but the title)."""
    read = item.get("read") if isinstance(item.get("read"), dict) else None
    if read is None:  # read before read records existed: done as far as anyone can tell
        return "done" if item.get("extract_v") else "pending"
    if "retry_at" in read:
        return "pending" if extract.pending(read, now) else "partial"
    status = str(read.get("status"))
    return "done" if status in DONE else "title" if status == "title_only" else "partial"


def _seen(public, source: str, start: int, end: int, since: int | None = None,
          now: float | None = None) -> tuple[int | None, int, Counter]:
    """(newest first_seen of a notice that was no seed row, those first seen in [start, end), how they were
    read: done, partial, pending, title, and expired: first seen QUEUE_DAYS before the period, never read, and
    not before `since`, when read records began)."""
    now = end if now is None else now
    # only these attributes: the role's Query grant allows nothing else (dynamodb:Attributes, dynamodb:Select)
    names = {f"#a{i}": name for i, name in enumerate(QUERY_ATTRIBUTES)}
    kwargs = {"KeyConditionExpression": Key("PK").eq(f"SRC#{source}") & Key("SK").begins_with("N#"),
              "Select": "SPECIFIC_ATTRIBUTES", "ProjectionExpression": ", ".join(names),
              "ExpressionAttributeNames": names}
    newest, fresh, health = None, 0, Counter()
    lag = QUEUE_DAYS * 86400
    while True:
        page = public.query(**kwargs)
        for item in page["Items"]:
            seen = int(item.get("first_seen") or 0)
            if not seen or item.get("seed"):  # a seed row (a board's first read) is no sign the board is alive
                continue
            newest = seen if newest is None else max(newest, seen)
            if item.get("removed"):
                continue
            if start <= seen < end:
                fresh += 1
                health[_bucket(item, now)] += 1
            elif (since is not None and max(start - lag, since) <= seen < end - lag
                  and not item.get("read") and not item.get("extract_v")):  # never read (a retry still planned isn't)
                health["expired"] += 1
        if "LastEvaluatedKey" not in page:
            return newest, fresh, health
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def queue_health(public, now: datetime) -> dict[str, int]:
    """The page queue: rows waiting and the oldest one's age in hours."""
    # only these attributes, and a projection always (the role's grant requires both)
    names = {f"#q{i}": name for i, name in enumerate(QUEUE_ATTRIBUTES)}
    kwargs = {"KeyConditionExpression": Key("PK").eq("BODYQ"), "Select": "SPECIFIC_ATTRIBUTES",
              "ProjectionExpression": ", ".join(names), "ExpressionAttributeNames": names}
    ages = []
    while True:
        page = public.query(**kwargs)
        ages += [int(now.timestamp()) - int(i.get("queued_at") or now.timestamp()) for i in page["Items"]]
        if "LastEvaluatedKey" not in page:
            return {"queue": len(ages), "oldest_h": max(ages, default=0) // 3600}
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def boards(public, now: datetime, start: datetime, end: datetime) -> dict:
    ts = int(now.timestamp())
    off, paused, stale, new, health = [], [], 0, 0, Counter()
    since = extract.reads_since(public)
    for origin in origins():
        state = politeness.state(public, origin)
        if state.get("disabled"):
            off.append(origin)
        elif int(state.get("cooldown_until", 0)) > ts:
            paused.append(origin)
    for source in public_data.SOURCES:
        state = public.get_item(Key={"PK": f"SRC#{source}", "SK": "STATE"}).get("Item") or {}
        if state.get("disabled"):
            off.append(source)
        elif int(state.get("not_before", 0)) > ts:
            paused.append(source)
        if source in NOT_WATCHED:
            continue
        newest, fresh, read = _seen(public, source, int(start.timestamp()), int(end.timestamp()), since, ts)
        new += fresh
        health += read
        if newest is None or ts - newest > STALE_DAYS.get(source, DEFAULT_STALE_DAYS) * 86400:
            stale += 1
    return {"off": off, "paused": paused, "stale": stale, "new": new,
            "read": {k: health[k] for k in ("done", "partial", "pending", "title", "expired")}}


def metric_sums(cw, start: datetime, end: datetime) -> dict[str, int]:
    """Sums of the KwCampus counters (published without dimensions) over the period."""
    seconds = int((end - start).total_seconds())
    queries = [{"Id": f"m{i}", "ReturnData": True, "MetricStat": {
        "Metric": {"Namespace": NAMESPACE, "MetricName": name}, "Period": seconds, "Stat": "Sum"}}
        for i, name in enumerate(EXTRACT_METRICS)]
    sums = dict.fromkeys(EXTRACT_METRICS, 0)
    kwargs = {"MetricDataQueries": queries, "StartTime": start, "EndTime": end}
    while True:
        page = cw.get_metric_data(**kwargs)
        for result in page.get("MetricDataResults") or []:
            name = EXTRACT_METRICS[int(str(result["Id"])[1:])]
            sums[name] += int(sum(result.get("Values") or []))
        if not page.get("NextToken"):
            return sums
        kwargs["NextToken"] = page["NextToken"]


def queues(sqs) -> dict[str, int]:
    def count(url: str, *names: str) -> int:
        attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=list(names))["Attributes"]
        return sum(int(attrs.get(n) or 0) for n in names)

    return {"waiting": count(DISCORD_QUEUE_URL, "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"),
            "dead": count(os.environ["KW_DISCORD_DLQ_URL"], "ApproximateNumberOfMessages")}


def invite_counts(bridge, dates: list[str]) -> dict[str, int]:
    return {"period": sum(invites.day_count(bridge, day) for day in dates), "total": invites.totals(bridge)["n"]}


def alarms(cw) -> list[str]:
    names, kwargs = [], {"AlarmNamePrefix": "kw-", "StateValue": "ALARM"}
    while True:
        page = cw.describe_alarms(**kwargs)
        names += [str(a["AlarmName"]).removeprefix("kw-") for a in page.get("MetricAlarms") or []]
        if not page.get("NextToken"):
            return sorted(names)
        kwargs["NextToken"] = page["NextToken"]


def _names(items: list[str]) -> str:
    shown = ", ".join(items[:MAX_NAMES])
    return f" ({shown}{f' 외 {len(items) - MAX_NAMES}' if len(items) > MAX_NAMES else ''})" if items else ""


def reading_line(b: dict | None, rq: dict | None) -> str:
    """How the period's new notices were read (boards' "read"), and the page queue (queue_health)."""
    if b is None or rq is None or "read" not in b:
        return UNREAD
    r, swept = b["read"], rq.get("sweep", "unread")
    rest = (UNREAD if swept == "unread" else "미집계" if swept is None
            else f"{swept[0]}{'' if swept[1] else '+'}")  # +: not every source walked yet
    return (f"새 {b['new']} · 완료 {r['done']} · 일부 {r['partial']} · 대기 {r['pending']} · 제목만 {r['title']} · "
            f"큐 {rq['queue']}(가장 오래 {rq['oldest_h']}시간) · 못 읽고 만료 {r['expired']} · 다시 읽기 남음 {rest}")


def compose(label: str, b: dict | None, x: dict | None, q: dict | None, inv: dict | None,
            alarm_names: list[str] | None, rq: dict | None = None) -> tuple[str, bool]:
    """(message, whether everything is fine). None for a part that couldn't be read. rq: the page queue; the
    reading line comes when the boards part carries "read" (boards())."""
    failed = None if x is None else x["ExtractFail"] + x["ExtractRefused"]
    reading = b is not None and "read" in b
    # quiet boards are normal (department boards often go weeks without a post): a count only, never a warning;
    # a notice whose queue row ran out before it was read is one to look at
    fine = (b is not None and not (b["off"] or b["paused"])
            and failed == 0
            and q is not None and q["dead"] == 0 and q["waiting"] < BACKLOG_WARN
            and inv is not None and alarm_names == []
            and (not reading or (rq is not None and b["read"]["expired"] == 0)))
    if fine:
        first = f"✅ {label} 이상 없음 · 새 공지 {b['new']} · 초대 {inv['period']}"
        return (first + f"\n읽기: {reading_line(b, rq)}" if reading else first), True
    lines = [f"⚠️ {label} 확인할 게 있어요"]
    lines.append("- 게시판: " + (UNREAD if b is None else " · ".join((
        f"꺼짐 {len(b['off'])}{_names(b['off'])}", f"잠시 멈춤 {len(b['paused'])}{_names(b['paused'])}",
        f"조용한 게시판 {b['stale']}", f"새 공지 {b['new']}"))))
    lines.append("- 공지 읽기: " + (UNREAD if x is None else
                                f"완료 {x['ExtractOk']} · 실패 {failed} · 한도로 미룸 {x['ExtractSkippedCap']}"))
    if reading or b is None:
        lines.append("- 읽기 상태: " + reading_line(b, rq))
    lines.append("- 디스코드: " + (UNREAD if q is None else
                               f"대기 {q['waiting']} · 실패 {q['dead']}"
                               + ("" if x is None else f" · 재시도 {x['DiscordRetries']}")))
    lines.append("- 초대: " + (UNREAD if inv is None else f"{inv['period']} (전체 {inv['total']})"))
    lines.append("- 알람: " + (UNREAD if alarm_names is None else ", ".join(alarm_names) or "없음"))
    return "\n".join(lines), False


def _part(name: str, read):
    try:
        return read()
    except Exception as e:  # noqa: BLE001 - one part that can't be read must not keep the rest from going out
        log("ops_digest_part_failed", part=name, error=type(e).__name__)
        return None


def run(now: datetime, public, bridge, cw, sqs) -> dict[str, object]:
    start, end, label, dates = period(now)
    b = _part("boards", lambda: boards(public, now, start, end))
    x = _part("metrics", lambda: metric_sums(cw, start, end))
    q = _part("queues", lambda: queues(sqs))
    inv = _part("invites", lambda: invite_counts(bridge, dates))
    alarm_names = _part("alarms", lambda: alarms(cw))
    rq = _part("page_queue", lambda: queue_health(public, now))
    swept = _part("sweep", lambda: sweep.remaining(public) or "never")  # its own part: it can't hide the queue's
    if rq is not None:
        rq = {**rq, "sweep": None if swept == "never" else swept if swept is not None else "unread"}
    text, fine = compose(label, b, x, q, inv, alarm_names, rq)
    if b is not None and rq is not None:
        r = b["read"]
        metrics(DigestReadNew=b["new"], DigestReadDone=r["done"], DigestReadPartial=r["partial"],
                DigestReadPending=r["pending"], DigestReadTitleOnly=r["title"], DigestReadExpired=r["expired"],
                DigestPageQueue=rq["queue"], DigestPageQueueOldestHours=rq["oldest_h"])
    today = now.astimezone(KST).date().isoformat()
    send_jobs(sqs, [{"action": "ops", "channel": "ops", "content": text, "id": f"digest|{today}"}])
    unread = sum(part is None for part in (b, x, q, inv, alarm_names, rq))
    log("ops_digest", fine=fine, unread=unread, alarms=len(alarm_names or []))
    metrics(OpsDigestFine=int(fine))
    return {"status": "ok", "fine": fine, "unread": unread}


def handler(event, context):
    dynamodb = boto3.resource("dynamodb")
    return run(datetime.now(UTC), dynamodb.Table(PUBLIC_TABLE), dynamodb.Table(invites.BRIDGE_TABLE),
               boto3.client("cloudwatch"), boto3.client("sqs"))
