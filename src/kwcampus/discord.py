"""Lambda: post or edit Discord messages for notice jobs from SQS.

The bot token comes from SSM (SecureString), channel ids from /kw/discord/channels and the
subscription role ids from /kw/discord/roles. A delivery record (kw-delivery) is claimed before
posting so an SQS retry never posts the same notice twice.

- post: the notice, with one line of what extract.py read on its page when that is already there and
  sure enough ("📌 대상: … · ⏰ 11/18(수) 24:00 마감 · 신청: …"; usually it is read minutes after the post,
  and a posted message is never edited just to add it), mentioning its topic role (none 22:00-08:00)
  and, for urgent titles from
  school-wide boards, the 긴급 role at any hour. A topic role is mentioned at most once per 30
  minutes (긴급 always). Only those roles may ping; never @everyone, @here or users. A title that
  may carry a person's name goes out as "새 글이 올라왔어요" with the link only, and pings nobody.
- edit: the original message is rewritten, in whatever channel it was posted to. If the school
  changed the title or the 수정일 and the post is more than 30 minutes old, a short "공지가 수정됐어요" reply
  goes under it, at most once per notice per KST day. Our own changes (new redaction rules, a
  recategorised notice) are quiet: the message is fixed, nobody is told.
- repost: a department board reused an old post for a new one; edit it if we posted it, else post.
- keep: the notice changed in a way the message doesn't show (pinned, department), or while
  notifications were off; only the delivery record's ttl moves, Discord isn't called (so neither a
  missing bot token nor a 429 holds it up).
- remind (reminders.py, weekday mornings): a reply under the notice's message, "⏰ 신청 마감 D-3 ·
  11/18(수) 24:00까지", mentioning nobody. Each D-level is claimed in the delivery record (remind_d3, remind_d1)
  before posting, so it goes out at most once; nothing goes out when the notice's 신청마감 changed
  since the job was made, or the message is gone (that level is then marked gone).
- ops (opsdigest.py, rulewatch.py): a short operator message to the private #운영 channel (the "ops"
  key of /kw/discord/channels) and nowhere else, mentioning nobody. Its id becomes the message nonce, so
  an SQS retry within Discord's nonce window doesn't post it twice. No channel set yet: dropped.
A notice taken down by hand (removed=true) is never posted or edited again, whatever job comes late.

The delivery record lives 30 days longer than its notice: each edit, repost and keep moves its ttl
along with the notice's, so a late takedown or edit still finds the message. Messages themselves
stay in Discord until a takedown deletes them.

After a 429 a record waits in this invocation when Discord asks for 5 s or less and there is time
left, and goes again; otherwise it and the rest of the batch go back to SQS, visible again once
Discord's wait is over (not after the queue's 180 s), so a redrive drains at Discord's pace.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from datetime import UTC, datetime, timedelta

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from kwcampus import params
from kwcampus.config import (
    DELIVERY_TABLE,
    DELIVERY_TTL,
    DISCORD_CHANNELS_PARAM,
    DISCORD_ROLES_PARAM,
    DISCORD_TOKEN_PARAM,
    EDIT_REPLY_AFTER,
    KST,
    NOTICE_TTL,
    PUBLIC_TABLE,
    QUIET_FROM_HOUR,
    QUIET_UNTIL_HOUR,
)
from kwcampus.discordapi import (
    NotFound,
    RateLimited,
    RetryLater,
    clean_token,
    is_snowflake,
    token_problem,
)
from kwcampus.discordapi import call as _discord
from kwcampus.extract import deadline_of, next_application, parse_at
from kwcampus.obs import log, metrics
from kwcampus.routing import (
    CATEGORY_LABEL,
    URGENT,
    is_urgent,
    name_risk,
    topic_for,
    urgent_source,
)

SUPPRESS_EMBEDS = 1 << 2
MAX_LEN = 2000
DISCORD_EPOCH_MS = 1420070400000
# A claim older than this belongs to an invocation that died (the Lambda timeout is 60 s), so an
# SQS retry may take it over; a younger one is still in flight and a duplicate job leaves it alone.
STALE_CLAIM = timedelta(seconds=90)
# One event shows up on several boards (a job fair on five department sites): a topic role is
# pinged at most this often; later notices still go out, just without the mention.
MENTION_EVERY = timedelta(minutes=30)
EDIT_NOTE_PREFIX = "✏️ 공지가 수정됐어요"
REMIND_PREFIX = "⏰ 신청 마감"
SUMMARY_MIN_CONFIDENCE = 0.7
_WEEKDAYS = "월화수목금토일"
SAFE_TITLE = "새 글이 올라왔어요. 제목은 링크를 눌러 확인해 주세요."
NO_PINGS = {"parse": []}
# A delivery record outlives its notice (kw-public ttl) by this much.
RECORD_GRACE = DELIVERY_TTL - NOTICE_TTL
# A 429 asking for at most this long is waited out in the invocation (Lambda timeout 60 s) ...
RATE_LIMIT_WAIT_MAX = 5.0
# ... when this much time is left after the wait: one job makes up to three Discord calls of 10 s
RATE_LIMIT_MARGIN = 20.0
RATE_LIMIT_TRIES = 3  # tries per record within one invocation
VISIBILITY_MAX = 43200  # SQS's limit (12 h)

_MARKDOWN = re.compile(r"([\\*_~`|>\[\]()#])")


def escape(text: str) -> str:
    return _MARKDOWN.sub(r"\\\1", text)


def is_quiet(now: datetime) -> bool:
    hour = now.astimezone(KST).hour
    return hour >= QUIET_FROM_HOUR or hour < QUIET_UNTIL_HOUR


def _risky(item: dict) -> bool:
    return name_risk(str(item.get("category_id", "")), str(item.get("title") or ""))


def mentions_for(item: dict, roles: dict[str, str] | None, now: datetime) -> list[str]:
    """Role ids to ping: the topic role by day, the 긴급 role at any hour for an urgent school-wide
    notice, nobody for a title that may name someone."""
    if _risky(item):
        return []
    roles = roles or {}
    category = str(item.get("category_id", ""))
    wanted = [] if is_quiet(now) else [roles.get(topic_for(category) or "")]
    if urgent_source(category) and is_urgent(str(item.get("title") or "")):
        wanted.append(roles.get(URGENT))
    return list(dict.fromkeys(str(r) for r in wanted if is_snowflake(r)))


def _nonce(*parts: str) -> str:
    """Discord drops a second message with the same nonce sent within a few minutes (enforce_nonce)."""
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:25]


def _when(at: str) -> str:
    """"11/18(수) 24:00" (a deadline at the day's end is stored as 23:59:59), or "11/18(수)"."""
    day, timed = parse_at(at)
    when = f"{day.month}/{day.day}({_WEEKDAYS[day.weekday()]})"
    if timed:
        clock = datetime.fromisoformat(at).astimezone(KST)
        when += " 24:00" if (clock.hour, clock.minute, clock.second) == (23, 59, 59) else f" {clock:%H:%M}"
    return when


def summary_line(item: dict, now: datetime) -> str | None:
    """"📌 대상: … · ⏰ 11/18(수) 24:00 마감 · 신청: …" from the notice's extracted facts; None without them,
    when the model was unsure, or for a title that may name someone."""
    facts = item.get("extracted")
    if not isinstance(facts, dict) or _risky(item):
        return None
    try:
        if float(facts.get("confidence") or 0) < SUMMARY_MIN_CONFIDENCE:
            return None
    except (TypeError, ValueError):
        return None
    today = now.astimezone(KST).date()
    deadline = next_application(facts, today) or deadline_of(facts, today)
    if deadline and parse_at(deadline["at"])[0] < today:
        deadline = None
    target, how = (" ".join(str(facts.get(k) or "").split()) for k in ("target", "how_to_apply"))
    parts = [f"대상: {escape(target)}" if target else "", f"⏰ {_when(deadline['at'])} 마감" if deadline else "",
             f"신청: {escape(how)}" if how else ""]
    parts = [p for p in parts if p]
    return "📌 " + " · ".join(parts) if parts else None


def render(item: dict, mentions: list[str] | None = None, now: datetime | None = None) -> str:
    label = CATEGORY_LABEL.get(str(item.get("category_id")), "공지")
    title = SAFE_TITLE if _risky(item) else escape(str(item["title"]))
    lines = [f"**[{label}]** {title}"]
    if summary := summary_line(item, now or datetime.now(UTC)):
        lines.append(summary)
    posted, updated = str(item.get("posted_date") or ""), str(item.get("updated_date") or "")
    when = f"{posted} (수정 {updated})" if updated and posted and updated != posted else posted
    # dates are our own ISO strings; only the department comes from the site
    meta = " · ".join(x for x in [escape(str(item.get("department") or "")), when] if x)
    if meta:
        lines.append(meta)
    lines.append(f"<{item['url']}>")
    tail = "\n" + " ".join(f"<@&{m}>" for m in mentions) if mentions else ""
    return "\n".join(lines)[: MAX_LEN - len(tail)] + tail


def render_edit_note(item: dict, changed: list[str], now: datetime) -> str:
    """The reply under an edited notice. Boards without a 수정일 show the day the change was seen."""
    posted, updated = str(item.get("posted_date") or ""), str(item.get("updated_date") or "")
    day = updated if updated and updated != posted else now.astimezone(KST).date().isoformat()
    head = f"{EDIT_NOTE_PREFIX} · {day}" + (" · 제목이 바뀌었어요" if "title" in changed else "")
    return f"{head}\n<{item['url']}>"


def render_reminder(at: str, now: datetime) -> str | None:
    """"⏰ 신청 마감 D-3 · 11/18(수) 24:00까지"; None once the day has passed. A deadline at the day's end is
    stored as 23:59:59 and shown as 24:00."""
    parsed = parse_at(at)
    if parsed is None:
        return None
    days_left = (parsed[0] - now.astimezone(KST).date()).days
    if days_left < 0:
        return None
    return f"{REMIND_PREFIX} {'D-day' if days_left == 0 else f'D-{days_left}'} · {_when(at)}까지"


def posted_at(message_id: str) -> datetime | None:
    """When Discord created the message (snowflake timestamp)."""
    if not is_snowflake(message_id):
        return None
    return datetime.fromtimestamp(((int(message_id) >> 22) + DISCORD_EPOCH_MS) / 1000, UTC)


def _throttle(delivery, mentions: list[str], always: set[str], notice: str, now: datetime) -> list[str]:
    """Keep a role only if it wasn't pinged in the last 30 minutes for another notice (a retry of the
    same notice keeps its slot). Roles in `always` (긴급) are never held back."""
    kept, ts = [], int(now.timestamp())
    for role in mentions:
        if role in always:
            kept.append(role)
            continue
        try:
            delivery.update_item(
                Key={"PK": f"ROLE#{role}", "SK": "PING"},
                UpdateExpression="SET last_ping = :now, last_notice = :me, #ttl = :ttl",
                ConditionExpression="attribute_not_exists(last_ping) OR last_ping < :cutoff OR last_notice = :me",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={":now": ts, ":me": notice, ":cutoff": ts - int(MENTION_EVERY.total_seconds()),
                                           ":ttl": ts + 86400},
            )
            kept.append(role)
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    return kept


def _find_record(delivery, dkey: dict) -> tuple[dict, dict | None]:
    """The delivery record that holds the message: the job's channel first, else whichever channel the
    notice was posted to (its category moved since). A notice is posted to one channel only."""
    record = delivery.get_item(Key=dkey, ConsistentRead=True).get("Item")
    if record and record.get("message_id"):
        return dkey, record
    rows = delivery.query(KeyConditionExpression=Key("PK").eq(dkey["PK"]) & Key("SK").begins_with("CH#"),
                          ConsistentRead=True).get("Items", [])
    for row in rows:
        if row.get("message_id"):
            return {"PK": row["PK"], "SK": row["SK"]}, row
    return dkey, None


def _claim_reply(delivery, dkey: dict, today: str, now: datetime) -> str | None:
    """Take today's reply slot for this notice: "new", "takeover" (an earlier attempt today died,
    maybe after Discord got its reply), or None when today's reply is out or in flight."""
    try:
        old = delivery.update_item(
            Key=dkey,
            UpdateExpression="SET reply_day = :day, reply_state = :claimed, reply_claimed_at = :now",
            ConditionExpression=(
                "attribute_not_exists(reply_day) OR reply_day <> :day "
                "OR (reply_state = :claimed AND reply_claimed_at < :stale)"
            ),
            ExpressionAttributeValues={
                ":day": today,
                ":claimed": "claimed",
                ":now": int(now.timestamp()),
                ":stale": int((now - STALE_CLAIM).timestamp()),
            },
            ReturnValues="ALL_OLD",  # the whole item before, unchanged values included
        ).get("Attributes") or {}
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise
    return "takeover" if old.get("reply_day") == today and old.get("reply_state") == "claimed" else "new"


def _earlier_reply(send, token: str, channel: str, message_id: str, today: str,
                   prefix: str = EDIT_NOTE_PREFIX) -> str | None:
    """Today's reply (edit note or reminder) under message_id, if a dead attempt already posted it
    (the latest 50 messages)."""
    recent = send("GET", f"/channels/{channel}/messages?limit=50", token)
    for m in recent if isinstance(recent, list) else []:
        sent = posted_at(str(m.get("id")))
        if ((m.get("message_reference") or {}).get("message_id") == message_id
                and str(m.get("content", "")).startswith(prefix)
                and sent and sent.astimezone(KST).date().isoformat() == today):
            return str(m["id"])
    return None


def _record_ttl(item: dict, now: datetime) -> int:
    """When a delivery record may go: 150 days from now, and never before its notice's ttl + 30 days
    (store.upsert moves that one to 120 days after each change)."""
    try:
        notice_ttl = int(item.get("ttl") or 0)
    except (TypeError, ValueError):
        notice_ttl = 0
    return max(int((now + DELIVERY_TTL).timestamp()), notice_ttl + int(RECORD_GRACE.total_seconds()))


def _keep_record(delivery, dkey: dict, item: dict, now: datetime) -> None:
    """Move the record's ttl forward with its notice's; a record deleted meanwhile (a takedown) stays gone."""
    try:
        delivery.update_item(
            Key=dkey, UpdateExpression="SET #ttl = :ttl",
            ConditionExpression="attribute_exists(PK) AND (attribute_not_exists(#ttl) OR #ttl < :ttl)",
            ExpressionAttributeNames={"#ttl": "ttl"}, ExpressionAttributeValues={":ttl": _record_ttl(item, now)},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


def _release(delivery, dkey: dict, update: str, condition: str, values: dict, names: dict | None = None) -> None:
    """Give up a claim this attempt still holds (after a 429); someone else's is left alone."""
    try:
        delivery.update_item(Key=dkey, UpdateExpression=update, ConditionExpression=condition,
                             ExpressionAttributeValues=values, **({"ExpressionAttributeNames": names} if names else {}))
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


def _post(item, dkey, channel_id, delivery, token, roles, send, now) -> str:
    claimed_at = int(now.timestamp())
    try:
        delivery.put_item(
            Item={**dkey, "status": "claimed", "channel_id": channel_id, "claimed_at": claimed_at,
                  "ttl": _record_ttl(item, now)},
            # a duplicate job while the first is still posting must not post (and ping) again
            ConditionExpression=(
                "attribute_not_exists(PK) OR (#s = :claimed AND attribute_not_exists(message_id) "
                "AND (attribute_not_exists(claimed_at) OR claimed_at < :stale))"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":claimed": "claimed", ":stale": int((now - STALE_CLAIM).timestamp())},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return "already_posted"
        raise
    urgent_role = {str((roles or {}).get(URGENT))}
    mentions = _throttle(delivery, mentions_for(item, roles, now), urgent_role, dkey["PK"], now)
    # Only these roles may ping: never @everyone, @here, users or other roles.
    allowed = {"parse": [], "roles": mentions} if mentions else NO_PINGS
    try:
        message = send("POST", f"/channels/{channel_id}/messages", token, {
            "content": render(item, mentions, now), "allowed_mentions": allowed, "flags": SUPPRESS_EMBEDS,
            "nonce": _nonce(dkey["PK"], dkey["SK"]), "enforce_nonce": True})
    except RateLimited:
        # Discord refused it, so nothing was posted: free the claim, or the retry a few seconds from
        # now would take it for a post still in flight and skip the notice ("already_posted").
        _release(delivery, dkey, "SET claimed_at = :zero",
                 "#s = :claimed AND attribute_not_exists(message_id) AND claimed_at = :mine",
                 {":zero": 0, ":claimed": "claimed", ":mine": claimed_at}, {"#s": "status"})
        raise
    values = {":sent": "sent", ":mid": str(message["id"])}
    update = "SET #s = :sent, message_id = :mid"
    if mentions:  # kept so later edits show the same roles and night posts stay without them
        update += ", mentions = :m"
        values[":m"] = mentions
    delivery.update_item(Key=dkey, UpdateExpression=update,
                         ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues=values)
    return "posted"


def _edit(job, item, dkey, record, delivery, token, send, now) -> str:
    message_id, record_channel = str(record["message_id"]), str(record["channel_id"])
    mentions = [str(m) for m in record.get("mentions") or [] if is_snowflake(m)]
    if _risky(item):
        mentions = []  # the title now may name someone: drop the mention with it
    try:
        # Edits don't notify anyone; the same roles stay allowed so the message keeps them as mentions.
        send("PATCH", f"/channels/{record_channel}/messages/{message_id}", token,
             {"content": render(item, mentions, now),
              "allowed_mentions": {"parse": [], "roles": mentions} if mentions else NO_PINGS})
    except NotFound:
        return "message_gone"  # someone deleted it; don't bring it back
    _keep_record(delivery, dkey, item, now)  # the notice's ttl moved with this change; the record follows
    if job.get("quiet"):
        return "edited_quietly"  # our own change (redaction rules, category): nothing to tell readers
    sent_at = posted_at(message_id)
    if sent_at and now - sent_at < EDIT_REPLY_AFTER:
        return "edited"  # a quick fix right after posting needs no note
    today = now.astimezone(KST).date().isoformat()
    claim = _claim_reply(delivery, dkey, today, now)
    if claim is None:
        return "edited"  # today's note is already there
    try:
        note_id = _earlier_reply(send, token, record_channel, message_id, today) if claim == "takeover" else None
        if note_id is None:
            note_id = str(send("POST", f"/channels/{record_channel}/messages", token, {
                "content": render_edit_note(item, job.get("changed") or ["title"], now),
                "allowed_mentions": {"parse": [], "replied_user": False},
                "flags": SUPPRESS_EMBEDS,
                "message_reference": {"message_id": message_id, "channel_id": record_channel,
                                      "fail_if_not_exists": False},
                "nonce": _nonce(dkey["PK"], dkey["SK"], "edit", today), "enforce_nonce": True,
            })["id"])
    except RateLimited:
        # Discord refused this call: mark today's reply slot stale so the retry takes it over (it looks
        # for a note an earlier attempt may have posted, then posts) instead of skipping the note.
        _release(delivery, dkey, "SET reply_claimed_at = :zero",
                 "reply_day = :day AND reply_state = :claimed AND reply_claimed_at = :mine",
                 {":zero": 0, ":day": today, ":claimed": "claimed", ":mine": int(now.timestamp())})
        raise
    delivery.update_item(
        Key=dkey,
        UpdateExpression="SET reply_state = :sent, reply_message_id = :mid",
        ExpressionAttributeValues={":sent": "sent", ":mid": note_id},
    )
    return "edited_replied"


def _claim_remind(delivery, dkey: dict, level: int, now: datetime) -> str | None:
    """Take one D-level's reminder: "new", "takeover" (an attempt that died, maybe after Discord got
    the reply) or None when it is out, gone or in flight."""
    ts = int(now.timestamp())
    try:
        old = delivery.update_item(
            Key=dkey, UpdateExpression="SET #r = :claimed, #at = :now",
            ConditionExpression="attribute_not_exists(#r) OR (#r = :claimed AND #at < :stale)",
            ExpressionAttributeNames={"#r": f"remind_d{level}", "#at": f"remind_d{level}_at"},
            ExpressionAttributeValues={":claimed": "claimed", ":now": ts, ":stale": ts - int(STALE_CLAIM.total_seconds())},
            ReturnValues="ALL_OLD",
        ).get("Attributes") or {}
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise
    return "takeover" if old.get(f"remind_d{level}") == "claimed" else "new"


def _remind(job, item, dkey, record, delivery, token, send, now) -> str:
    deadline = next_application(item.get("extracted"), now.astimezone(KST).date())
    if deadline is None or deadline["at"] != job.get("at"):
        return "deadline_changed"  # re-read since the job was made; tomorrow's run decides again
    content = render_reminder(deadline["at"], now)
    if content is None:
        return "deadline_passed"
    claims = {level: c for level in sorted({int(x) for x in job.get("levels") or []}, reverse=True)
              if (c := _claim_remind(delivery, dkey, level, now))}
    if not claims:
        return "already_reminded"
    message_id, channel = str(record["message_id"]), str(record["channel_id"])
    ts = int(now.timestamp())

    def mark(state: str, extra: dict | None = None) -> None:
        for level in claims:
            names = {"#r": f"remind_d{level}", **({"#id": f"remind_d{level}_id"} if extra else {})}
            delivery.update_item(Key=dkey, UpdateExpression="SET #r = :s" + (", #id = :id" if extra else ""),
                                 ExpressionAttributeNames=names, ExpressionAttributeValues={":s": state, **(extra or {})})

    try:
        send("GET", f"/channels/{channel}/messages/{message_id}", token)  # still there?
        today = now.astimezone(KST).date().isoformat()
        note_id = (_earlier_reply(send, token, channel, message_id, today, REMIND_PREFIX)
                   if "takeover" in claims.values() else None)
        if note_id is None:
            note_id = str(send("POST", f"/channels/{channel}/messages", token, {
                "content": content,
                "allowed_mentions": {"parse": [], "replied_user": False},
                "flags": SUPPRESS_EMBEDS,
                "message_reference": {"message_id": message_id, "channel_id": channel, "fail_if_not_exists": True},
                "nonce": _nonce(dkey["PK"], "remind", deadline["at"], *map(str, claims)), "enforce_nonce": True,
            })["id"])
    except NotFound:
        mark("gone")  # deleted on Discord: no reminder can hang under it
        return "message_gone"
    except RateLimited:
        # Discord refused: free the claims so the retry takes them over right away
        for level in claims:
            _release(delivery, dkey, "SET #at = :zero", "#r = :claimed AND #at = :mine",
                     {":zero": 0, ":claimed": "claimed", ":mine": ts},
                     {"#r": f"remind_d{level}", "#at": f"remind_d{level}_at"})
        raise
    mark("sent", {":id": note_id})
    return "reminded"


def _ops(job: dict, token: str, channels: dict[str, str], send) -> str:
    """An operator message, only ever to the ops channel, with no mentions and no embeds."""
    channel = str(channels.get("ops") or "")
    if not is_snowflake(channel):
        return "no_channel"
    content = str(job.get("content") or "").strip()[:MAX_LEN]
    if not content:
        return "empty"
    send("POST", f"/channels/{channel}/messages", token, {
        "content": content, "allowed_mentions": NO_PINGS, "flags": SUPPRESS_EMBEDS,
        "nonce": _nonce("ops", str(job.get("id") or content)), "enforce_nonce": True})
    return "ops_posted"


def handle_job(job: dict, public, delivery, token: str, channels: dict[str, str],
               roles: dict[str, str] | None = None, send=_discord, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    action = job["action"]
    if action == "ops":  # no notice behind it: the ops channel only
        return _ops(job, token, channels, send)
    # A department channel is optional: until its id is set, department notices go to "notice".
    channel_id = channels.get(job["channel"]) or (channels.get("notice") if job["channel"] == "department" else None)
    if action == "post" and not channel_id:
        return "no_channel"
    item = public.get_item(Key=job["key"], ConsistentRead=True).get("Item")
    if not item:
        return "gone"
    if item.get("removed"):
        # taken down (collector_admin takedown): a late or redriven job must not post it again
        return "removed"
    dkey = {"PK": f"{job['key']['PK']}|{job['key']['SK']}", "SK": f"CH#{job['channel']}"}

    if action == "remind":
        found_key, record = _find_record(delivery, dkey)
        if not record:
            return "nothing_to_remind"  # never announced, or taken down
        return _remind(job, item, found_key, record, delivery, token, send, now)

    if action == "keep":  # the notice's ttl moved with nothing the message shows: no Discord call
        found_key, record = _find_record(delivery, dkey)
        if not record:
            return "nothing_to_keep"  # never posted, or taken down: a missing record stays missing
        _keep_record(delivery, found_key, item, now)
        return "kept"

    if action in ("edit", "repost"):
        found_key, record = _find_record(delivery, dkey)
        if record:
            return _edit({**job, "action": "edit"}, item, found_key, record, delivery, token, send, now)
        if action == "edit":
            return "nothing_to_edit"
        action = "post"  # a reused post we never announced is a new notice

    if action == "post":
        if not channel_id:
            return "no_channel"
        return _post(item, dkey, channel_id, delivery, token, roles, send, now)
    return "unknown_action"


def _waiting_out_429s(run_job, time_left, sleep) -> str:
    """run_job(), waiting in place when Discord asks for RATE_LIMIT_WAIT_MAX seconds or less and the
    invocation has the time; any other 429 goes up to the caller."""
    attempt = 1
    while True:
        try:
            return run_job()
        except RateLimited as e:
            wait = max(0.0, e.retry_after)
            if (attempt >= RATE_LIMIT_TRIES or wait > RATE_LIMIT_WAIT_MAX
                    or time_left() < wait + RATE_LIMIT_MARGIN):
                raise
            log("discord_rate_limited_wait", retry_after=wait, attempt=attempt)
            sleep(wait)
            attempt += 1


def _queue_url(arn: str) -> str | None:
    """arn:aws:sqs:<region>:<account>:<name> -> https://sqs.<region>.amazonaws.com/<account>/<name>"""
    parts = arn.split(":")
    if len(parts) != 6 or parts[:3] != ["arn", "aws", "sqs"]:
        return None
    return f"https://sqs.{parts[3]}.amazonaws.com/{parts[4]}/{parts[5]}"


def _come_back_after(records: list[dict], seconds: float) -> None:
    """Make these SQS messages visible again once Discord's wait is over instead of after the queue's
    180 s visibility timeout. Best effort: if this fails, they come back after the 180 s."""
    timeout = min(VISIBILITY_MAX, max(1, math.ceil(seconds)))
    by_queue: dict[str, list[dict]] = {}
    for record in records:
        url = _queue_url(str(record.get("eventSourceARN") or ""))
        if url and record.get("receiptHandle"):
            by_queue.setdefault(url, []).append(record)
    if not by_queue:
        return
    sqs = boto3.client("sqs")
    for url, rows in by_queue.items():
        for start in range(0, len(rows), 10):
            entries = [{"Id": str(i), "ReceiptHandle": r["receiptHandle"], "VisibilityTimeout": timeout}
                       for i, r in enumerate(rows[start:start + 10])]
            try:
                failed = sqs.change_message_visibility_batch(QueueUrl=url, Entries=entries).get("Failed") or []
            except Exception as e:  # noqa: BLE001 - the 180 s default still brings them back
                log("discord_visibility_failed", error=type(e).__name__, records=len(entries))
                continue
            if failed:
                log("discord_visibility_failed", error="partial", records=len(failed))


def handler(event, context, sleep=time.sleep):
    token = clean_token(params.get(DISCORD_TOKEN_PARAM, decrypt=True))
    channels = json.loads(params.get(DISCORD_CHANNELS_PARAM) or "{}")
    cfg = json.loads(params.get(DISCORD_ROLES_PARAM) or "{}")
    # never the @everyone role (its id is the guild's), only real role ids
    roles = {k: str(v) for k, v in (cfg.get("roles") or {}).items()
             if is_snowflake(v) and str(v) != str(cfg.get("guild"))}
    dynamodb = boto3.resource("dynamodb")
    public, delivery = dynamodb.Table(PUBLIC_TABLE), dynamodb.Table(DELIVERY_TABLE)
    # without a Lambda context (a test, a manual call) there is no time to wait in
    time_left = (lambda: context.get_remaining_time_in_millis() / 1000) if context else (lambda: 0.0)

    failures, outcomes = [], {}
    problem = token_problem(token)
    limited = None  # after a 429 we could not wait out, the rest of the batch waits for SQS too
    postponed: list[dict] = []  # those records, to come back when Discord's wait is over
    for record in event.get("Records", []):
        try:
            job = json.loads(record["body"])
            if job.get("action") != "keep":  # a keep job never calls Discord: no token or 429 holds it up
                if problem:
                    raise RetryLater(problem)
                if limited is not None:
                    postponed.append(record)
                    raise RetryLater("discord 429 earlier in this batch")
            outcome = _waiting_out_429s(
                lambda job=job: handle_job(job, public, delivery, token, channels, roles), time_left, sleep)
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        except RateLimited as e:
            limited = max(0.0, e.retry_after)
            postponed.append(record)
            log("discord_rate_limited", retry_after=e.retry_after)
            failures.append({"itemIdentifier": record["messageId"]})
        except RetryLater as e:
            log("discord_retry", reason=str(e))
            failures.append({"itemIdentifier": record["messageId"]})
        except Exception as e:  # noqa: BLE001 - one bad job must not fail the batch; DLQ after retries
            log("discord_error", error=type(e).__name__, detail=str(e)[:120])
            failures.append({"itemIdentifier": record["messageId"]})
    if limited is not None and postponed:
        _come_back_after(postponed, limited)
    log("discord_done", **outcomes, retries=len(failures))
    metrics(DiscordPosted=outcomes.get("posted", 0), DiscordEditNotes=outcomes.get("edited_replied", 0),
            DiscordReminders=outcomes.get("reminded", 0), DiscordRetries=len(failures))
    return {"batchItemFailures": failures}
