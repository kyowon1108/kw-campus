"""Lambda: one short message to the private #운영 channel, weekdays 08:40 KST.

It covers the previous KST day (on Monday: Friday to Sunday) and holds counts and the names of
boards, servers and alarms only: no notice titles, no Discord members, no invite hashes.

- boards: servers (politeness ORIGIN# state) and sources (SRC#<source> STATE) switched off or in a
  cool-down / back-off right now, sources with nothing new for a while (the newest first_seen of their
  notices: 3 days for the main board, 7 for the 취업게시판, 21 for the others; the FAQ isn't watched),
  and how many notices were new in the period;
- notice pages read (extract.py): done / failed / skipped for the day's cap, from the KwCampus metrics
  ExtractOk, ExtractFail + ExtractRefused and ExtractSkippedCap;
- Discord delivery: jobs waiting on kw-discord, jobs in kw-discord-dlq, the sender's retries;
- invites: the DINVITES#<date> counters of the period and the running total (DINVITES#total);
- CloudWatch alarms named kw-* in ALARM.

When nothing needs a look it is one line: "✅ 어제 이상 없음 · 새 공지 12 · 초대 3". A part that couldn't be
read says so ("확인 못 함") and counts as something to look at.

The message goes to the Discord queue as an "ops" job; the sender posts it to the "ops" channel of
/kw/discord/channels (scripts/discord_setup.py sets it). This function holds no Discord secret.
Reads: kw-public GetItem on ORIGIN#* and SRC#* (state rows), Query on SRC#* for first_seen, seed and
removed only (QUERY_ATTRIBUTES), kw-bridge GetItem on DINVITES#* only,
GetQueueAttributes on the two Discord queues, cloudwatch:GetMetricData and DescribeAlarms (kw-*).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import boto3
from boto3.dynamodb.conditions import Key

from kwcampus import deptboards, invites, politeness, public_data, wixfeeds
from kwcampus.config import DISCORD_QUEUE_URL, KST, NOTICE_SOURCE, PUBLIC_TABLE
from kwcampus.dispatcher import send_jobs
from kwcampus.kwnotice.source import NOTICE_HOST
from kwcampus.obs import NAMESPACE, log, metrics

STALE_DAYS = {NOTICE_SOURCE: 3, "kw-job": 7}
DEFAULT_STALE_DAYS = 21
NOT_WATCHED = frozenset({"kw-faq"})  # the FAQ changes rarely and is never announced
BACKLOG_WARN = 20  # jobs still waiting on kw-discord at 08:40 (nothing is collected overnight)
MAX_NAMES = 5
EXTRACT_METRICS = ("ExtractOk", "ExtractFail", "ExtractRefused", "ExtractSkippedCap", "DiscordRetries")
UNREAD = "확인 못 함"
QUERY_ATTRIBUTES = ("first_seen", "seed", "removed")  # all the notices' Query may return (infra grants just these)


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
    return sorted({NOTICE_HOST, *(b.server for b in deptboards.BOARDS), *(f.host for f in wixfeeds.FEEDS)})


def _seen(public, source: str, start: int, end: int) -> tuple[int | None, int]:
    """(newest first_seen, notices first seen in [start, end) that were no seed rows) of one source."""
    # only these attributes: the role's Query grant allows nothing else (dynamodb:Attributes, dynamodb:Select)
    names = {f"#a{i}": name for i, name in enumerate(QUERY_ATTRIBUTES)}
    kwargs = {"KeyConditionExpression": Key("PK").eq(f"SRC#{source}") & Key("SK").begins_with("N#"),
              "Select": "SPECIFIC_ATTRIBUTES", "ProjectionExpression": ", ".join(names),
              "ExpressionAttributeNames": names}
    newest, fresh = None, 0
    while True:
        page = public.query(**kwargs)
        for item in page["Items"]:
            seen = int(item.get("first_seen") or 0)
            if not seen:
                continue
            newest = seen if newest is None else max(newest, seen)
            fresh += start <= seen < end and not item.get("seed") and not item.get("removed")
        if "LastEvaluatedKey" not in page:
            return newest, fresh
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def boards(public, now: datetime, start: datetime, end: datetime) -> dict:
    ts = int(now.timestamp())
    off, paused, stale, new = [], [], 0, 0
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
        newest, fresh = _seen(public, source, int(start.timestamp()), int(end.timestamp()))
        new += fresh
        if newest is None or ts - newest > STALE_DAYS.get(source, DEFAULT_STALE_DAYS) * 86400:
            stale += 1
    return {"off": off, "paused": paused, "stale": stale, "new": new}


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


def compose(label: str, b: dict | None, x: dict | None, q: dict | None, inv: dict | None,
            alarm_names: list[str] | None) -> tuple[str, bool]:
    """(message, whether everything is fine). None for a part that couldn't be read."""
    failed = None if x is None else x["ExtractFail"] + x["ExtractRefused"]
    fine = (b is not None and not (b["off"] or b["paused"] or b["stale"])
            and failed == 0
            and q is not None and q["dead"] == 0 and q["waiting"] < BACKLOG_WARN
            and inv is not None and alarm_names == [])
    if fine:
        return f"✅ {label} 이상 없음 · 새 공지 {b['new']} · 초대 {inv['period']}", True
    lines = [f"⚠️ {label} 확인할 게 있어요"]
    lines.append("- 게시판: " + (UNREAD if b is None else " · ".join((
        f"꺼짐 {len(b['off'])}{_names(b['off'])}", f"잠시 멈춤 {len(b['paused'])}{_names(b['paused'])}",
        f"한동안 새 글 없음 {b['stale']}", f"새 공지 {b['new']}"))))
    lines.append("- 공지 읽기: " + (UNREAD if x is None else
                                f"완료 {x['ExtractOk']} · 실패 {failed} · 한도로 미룸 {x['ExtractSkippedCap']}"))
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
    text, fine = compose(label, b, x, q, inv, alarm_names)
    today = now.astimezone(KST).date().isoformat()
    send_jobs(sqs, [{"action": "ops", "channel": "ops", "content": text, "id": f"digest|{today}"}])
    unread = sum(part is None for part in (b, x, q, inv, alarm_names))
    log("ops_digest", fine=fine, unread=unread, alarms=len(alarm_names or []))
    metrics(OpsDigestFine=int(fine))
    return {"status": "ok", "fine": fine, "unread": unread}


def handler(event, context):
    dynamodb = boto3.resource("dynamodb")
    return run(datetime.now(UTC), dynamodb.Table(PUBLIC_TABLE), dynamodb.Table(invites.BRIDGE_TABLE),
               boto3.client("cloudwatch"), boto3.client("sqs"))
