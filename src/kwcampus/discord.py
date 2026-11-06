"""Lambda: post or edit Discord messages for notice jobs from SQS.

The bot token comes from SSM (SecureString), channel ids from /kw/discord/channels and the
subscription role ids from /kw/discord/roles. A delivery record (kw-delivery) is claimed before
posting so an SQS retry never posts the same notice twice.

- post: the notice, mentioning its topic role (none 22:00-08:00) and, for urgent titles, the
  긴급 role at any hour. Only those roles may ping; never @everyone, @here or users.
- edit: the original message is rewritten. If it was posted more than 30 minutes ago, a short
  "수정됨" reply under it tells readers, at most once per notice per KST day.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

import boto3
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
    PUBLIC_TABLE,
    QUIET_FROM_HOUR,
    QUIET_UNTIL_HOUR,
)
from kwcampus.discordapi import (
    NotFound,
    RetryLater,
    clean_token,
    is_snowflake,
    token_problem,
)
from kwcampus.discordapi import call as _discord
from kwcampus.obs import log, metrics
from kwcampus.routing import CATEGORY_LABEL, URGENT, is_urgent, topic_for

SUPPRESS_EMBEDS = 1 << 2
MAX_LEN = 2000
DISCORD_EPOCH_MS = 1420070400000
# A claim older than this belongs to an invocation that died (the Lambda timeout is 60 s), so an
# SQS retry may take it over; a younger one is still in flight and a duplicate job leaves it alone.
STALE_CLAIM = timedelta(seconds=90)
EDIT_NOTE_PREFIX = "✏️ 수정됨"
NO_PINGS = {"parse": []}

_MARKDOWN = re.compile(r"([\\*_~`|>\[\]()#])")


def escape(text: str) -> str:
    return _MARKDOWN.sub(r"\\\1", text)


def is_quiet(now: datetime) -> bool:
    hour = now.astimezone(KST).hour
    return hour >= QUIET_FROM_HOUR or hour < QUIET_UNTIL_HOUR


def mentions_for(item: dict, roles: dict[str, str] | None, now: datetime) -> list[str]:
    """Role ids to ping: the topic role by day, the 긴급 role at any hour when the title is urgent."""
    roles = roles or {}
    wanted = [] if is_quiet(now) else [roles.get(topic_for(str(item.get("category_id", ""))) or "")]
    if is_urgent(str(item.get("title") or "")):
        wanted.append(roles.get(URGENT))
    return list(dict.fromkeys(str(r) for r in wanted if is_snowflake(r)))


def _nonce(*parts: str) -> str:
    """Discord drops a second message with the same nonce sent within a few minutes (enforce_nonce)."""
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:25]


def render(item: dict, mentions: list[str] | None = None) -> str:
    label = CATEGORY_LABEL.get(str(item.get("category_id")), "공지")
    lines = [f"**[{label}]** {escape(str(item['title']))}"]
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
    head = f"{EDIT_NOTE_PREFIX} · {day}" + (" · 제목 변경" if "title" in changed else "")
    return f"{head}\n<{item['url']}>"


def posted_at(message_id: str) -> datetime | None:
    """When Discord created the message (snowflake timestamp)."""
    if not is_snowflake(message_id):
        return None
    return datetime.fromtimestamp(((int(message_id) >> 22) + DISCORD_EPOCH_MS) / 1000, UTC)


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


def _earlier_reply(send, token: str, channel: str, message_id: str, today: str) -> str | None:
    """Today's edit note under message_id, if a dead attempt already posted it (the latest 50 messages)."""
    recent = send("GET", f"/channels/{channel}/messages?limit=50", token)
    for m in recent if isinstance(recent, list) else []:
        sent = posted_at(str(m.get("id")))
        if ((m.get("message_reference") or {}).get("message_id") == message_id
                and str(m.get("content", "")).startswith(EDIT_NOTE_PREFIX)
                and sent and sent.astimezone(KST).date().isoformat() == today):
            return str(m["id"])
    return None


def handle_job(job: dict, public, delivery, token: str, channels: dict[str, str],
               roles: dict[str, str] | None = None, send=_discord, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    # A department channel is optional: until its id is set, department notices go to "notice".
    channel_id = channels.get(job["channel"]) or (channels.get("notice") if job["channel"] == "department" else None)
    if not channel_id:
        return "no_channel"
    item = public.get_item(Key=job["key"], ConsistentRead=True).get("Item")
    if not item:
        return "gone"
    dkey = {"PK": f"{job['key']['PK']}|{job['key']['SK']}", "SK": f"CH#{job['channel']}"}

    if job["action"] == "post":
        try:
            delivery.put_item(
                Item={**dkey, "status": "claimed", "channel_id": channel_id, "claimed_at": int(now.timestamp()),
                      "ttl": int((now + DELIVERY_TTL).timestamp())},
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
        mentions = mentions_for(item, roles, now)
        # Only these roles may ping: never @everyone, @here, users or other roles.
        allowed = {"parse": [], "roles": mentions} if mentions else NO_PINGS
        message = send("POST", f"/channels/{channel_id}/messages", token, {
            "content": render(item, mentions), "allowed_mentions": allowed, "flags": SUPPRESS_EMBEDS,
            "nonce": _nonce(dkey["PK"], dkey["SK"]), "enforce_nonce": True})
        values = {":sent": "sent", ":mid": str(message["id"])}
        update = "SET #s = :sent, message_id = :mid"
        if mentions:  # kept so later edits show the same roles and night posts stay without them
            update += ", mentions = :m"
            values[":m"] = mentions
        delivery.update_item(Key=dkey, UpdateExpression=update,
                             ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues=values)
        return "posted"

    if job["action"] == "edit":
        record = delivery.get_item(Key=dkey, ConsistentRead=True).get("Item")
        if not record or not record.get("message_id"):
            return "nothing_to_edit"
        message_id, record_channel = str(record["message_id"]), str(record["channel_id"])
        mentions = [str(m) for m in record.get("mentions") or [] if is_snowflake(m)]
        try:
            # Edits don't notify anyone; the same roles stay allowed so the message keeps them as mentions.
            send("PATCH", f"/channels/{record_channel}/messages/{message_id}", token,
                 {"content": render(item, mentions),
                  "allowed_mentions": {"parse": [], "roles": mentions} if mentions else NO_PINGS})
        except NotFound:
            return "message_gone"  # someone deleted it; don't bring it back
        sent_at = posted_at(message_id)
        if sent_at and now - sent_at < EDIT_REPLY_AFTER:
            return "edited"  # a quick fix right after posting needs no note
        today = now.astimezone(KST).date().isoformat()
        claim = _claim_reply(delivery, dkey, today, now)
        if claim is None:
            return "edited"  # today's note is already there
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
        delivery.update_item(
            Key=dkey,
            UpdateExpression="SET reply_state = :sent, reply_message_id = :mid",
            ExpressionAttributeValues={":sent": "sent", ":mid": note_id},
        )
        return "edited_replied"
    return "unknown_action"


def handler(event, context):
    token = clean_token(params.get(DISCORD_TOKEN_PARAM, decrypt=True))
    channels = json.loads(params.get(DISCORD_CHANNELS_PARAM) or "{}")
    cfg = json.loads(params.get(DISCORD_ROLES_PARAM) or "{}")
    # never the @everyone role (its id is the guild's), only real role ids
    roles = {k: str(v) for k, v in (cfg.get("roles") or {}).items()
             if is_snowflake(v) and str(v) != str(cfg.get("guild"))}
    dynamodb = boto3.resource("dynamodb")
    public, delivery = dynamodb.Table(PUBLIC_TABLE), dynamodb.Table(DELIVERY_TABLE)

    failures, outcomes = [], {}
    problem = token_problem(token)
    for record in event.get("Records", []):
        try:
            if problem:
                raise RetryLater(problem)
            outcome = handle_job(json.loads(record["body"]), public, delivery, token, channels, roles)
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        except RetryLater as e:
            log("discord_retry", reason=str(e))
            failures.append({"itemIdentifier": record["messageId"]})
        except Exception as e:  # noqa: BLE001 - one bad job must not fail the batch; DLQ after retries
            log("discord_error", error=type(e).__name__, detail=str(e)[:120])
            failures.append({"itemIdentifier": record["messageId"]})
    log("discord_done", **outcomes, retries=len(failures))
    metrics(DiscordPosted=outcomes.get("posted", 0), DiscordEditNotes=outcomes.get("edited_replied", 0),
            DiscordRetries=len(failures))
    return {"batchItemFailures": failures}
