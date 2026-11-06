"""Lambda: post or edit Discord messages for notice jobs from SQS.

The bot token comes from SSM (SecureString), channel ids from /kw/discord/channels and the
subscription role ids from /kw/discord/roles. A delivery record (kw-delivery) is claimed before
posting so an SQS retry never posts the same notice twice.

- post: the notice, mentioning its topic role (only that role may ping; none at night).
- edit: the original message is rewritten. If it was posted more than 30 minutes ago, a short
  "수정됨" reply under it tells readers, at most once per notice per KST day.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
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
from kwcampus.fetch import TLS
from kwcampus.obs import log, metrics
from kwcampus.routing import CATEGORY_LABEL, topic_for

API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/kyowon1108/kw-notice-mcp, 0.1)"
SUPPRESS_EMBEDS = 1 << 2
MAX_LEN = 2000
DISCORD_EPOCH_MS = 1420070400000
# A reply claim older than this belongs to an invocation that died (the Lambda timeout is 60 s),
# so an SQS retry may take it over.
STALE_CLAIM = timedelta(seconds=90)
NO_PINGS = {"parse": []}

_MARKDOWN = re.compile(r"([\\*_~`|>\[\]()#])")


class RetryLater(Exception):
    """Discord asked us to slow down or failed transiently; let SQS redeliver."""


class NotFound(Exception):
    """Discord answered 404: the message (or member, role, channel) is gone."""


def escape(text: str) -> str:
    return _MARKDOWN.sub(r"\\\1", text)


def is_quiet(now: datetime) -> bool:
    hour = now.astimezone(KST).hour
    return hour >= QUIET_FROM_HOUR or hour < QUIET_UNTIL_HOUR


def render(item: dict, mention: str | None = None) -> str:
    label = CATEGORY_LABEL.get(str(item.get("category_id")), "공지")
    lines = [f"**[{label}]** {escape(str(item['title']))}"]
    posted, updated = str(item.get("posted_date") or ""), str(item.get("updated_date") or "")
    when = f"{posted} (수정 {updated})" if updated and posted and updated != posted else posted
    # dates are our own ISO strings; only the department comes from the site
    meta = " · ".join(x for x in [escape(str(item.get("department") or "")), when] if x)
    if meta:
        lines.append(meta)
    lines.append(f"<{item['url']}>")
    tail = f"\n<@&{mention}>" if mention else ""
    return "\n".join(lines)[: MAX_LEN - len(tail)] + tail


def render_edit_note(item: dict, changed: list[str], now: datetime) -> str:
    """The reply under an edited notice. Boards without a 수정일 show the day the change was seen."""
    posted, updated = str(item.get("posted_date") or ""), str(item.get("updated_date") or "")
    day = updated if updated and updated != posted else now.astimezone(KST).date().isoformat()
    head = f"✏️ 수정됨 · {day}" + (" · 제목 변경" if "title" in changed else "")
    return f"{head}\n<{item['url']}>"


def posted_at(message_id: str) -> datetime | None:
    """When Discord created the message (snowflake timestamp)."""
    if not str(message_id).isdigit():
        return None
    return datetime.fromtimestamp(((int(message_id) >> 22) + DISCORD_EPOCH_MS) / 1000, UTC)


def token_problem(token: str | None) -> str | None:
    # Discord tokens are plain ASCII; anything else means the wrong text was pasted into SSM.
    if not token:
        return "bot token not set"
    if not token.isascii() or " " in token.strip():
        return "bot token is not a Discord token (non-ASCII or spaces)"
    return None


def _discord(method: str, path: str, token: str, body: dict | None = None, timeout: float = 10) -> dict:
    request = urllib.request.Request(
        f"{API}{path}",
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=TLS) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NotFound(f"discord 404 on {method}") from None
        if e.code == 429 or e.code >= 500:
            raise RetryLater(f"discord {e.code}") from None
        raise RuntimeError(f"discord {e.code} on {method}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise RetryLater("discord unreachable") from e


def _claim_reply(delivery, dkey: dict, now: datetime) -> bool:
    """Take today's reply slot for this notice; False when today's reply is already out or in flight."""
    try:
        delivery.update_item(
            Key=dkey,
            UpdateExpression="SET reply_day = :day, reply_state = :claimed, reply_claimed_at = :now",
            ConditionExpression=(
                "attribute_not_exists(reply_day) OR reply_day <> :day "
                "OR (reply_state = :claimed AND reply_claimed_at < :stale)"
            ),
            ExpressionAttributeValues={
                ":day": now.astimezone(KST).date().isoformat(),
                ":claimed": "claimed",
                ":now": int(now.timestamp()),
                ":stale": int((now - STALE_CLAIM).timestamp()),
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def handle_job(job: dict, public, delivery, token: str, channels: dict[str, str],
               roles: dict[str, str] | None = None, send=_discord, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    # A department channel is optional: until its id is set, department notices go to "notice".
    channel_id = channels.get(job["channel"]) or (channels.get("notice") if job["channel"] == "department" else None)
    if not channel_id:
        return "no_channel"
    item = public.get_item(Key=job["key"]).get("Item")
    if not item:
        return "gone"
    dkey = {"PK": f"{job['key']['PK']}|{job['key']['SK']}", "SK": f"CH#{job['channel']}"}

    if job["action"] == "post":
        try:
            delivery.put_item(
                Item={**dkey, "status": "claimed", "channel_id": channel_id,
                      "ttl": int((now + DELIVERY_TTL).timestamp())},
                ConditionExpression="attribute_not_exists(PK) OR (#s = :claimed AND attribute_not_exists(message_id))",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":claimed": "claimed"},
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return "already_posted"
            raise
        topic = topic_for(str(item.get("category_id", "")))
        role_id = (roles or {}).get(topic or "")
        mention = role_id if role_id and str(role_id).isdigit() and not is_quiet(now) else None
        # Only the topic role may ping: never @everyone, @here, users or other roles.
        allowed = {"parse": [], "roles": [mention]} if mention else NO_PINGS
        message = send("POST", f"/channels/{channel_id}/messages", token,
                       {"content": render(item, mention), "allowed_mentions": allowed, "flags": SUPPRESS_EMBEDS})
        values = {":sent": "sent", ":mid": str(message["id"])}
        update = "SET #s = :sent, message_id = :mid"
        if mention:  # kept so later edits show the same role and night posts stay without one
            update += ", mention = :m"
            values[":m"] = mention
        delivery.update_item(Key=dkey, UpdateExpression=update,
                             ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues=values)
        return "posted"

    if job["action"] == "edit":
        record = delivery.get_item(Key=dkey).get("Item")
        if not record or not record.get("message_id"):
            return "nothing_to_edit"
        message_id, record_channel = str(record["message_id"]), str(record["channel_id"])
        try:
            # Edits never ping, even though the mention stays in the text.
            send("PATCH", f"/channels/{record_channel}/messages/{message_id}", token,
                 {"content": render(item, record.get("mention")), "allowed_mentions": NO_PINGS})
        except NotFound:
            return "message_gone"  # someone deleted it; don't bring it back
        sent_at = posted_at(message_id)
        if sent_at and now - sent_at < EDIT_REPLY_AFTER:
            return "edited"  # a quick fix right after posting needs no note
        if not _claim_reply(delivery, dkey, now):
            return "edited"  # today's note is already there
        note = send("POST", f"/channels/{record_channel}/messages", token, {
            "content": render_edit_note(item, job.get("changed") or ["title"], now),
            "allowed_mentions": {"parse": [], "replied_user": False},
            "flags": SUPPRESS_EMBEDS,
            "message_reference": {"message_id": message_id, "channel_id": record_channel,
                                  "fail_if_not_exists": False},
        })
        delivery.update_item(
            Key=dkey,
            UpdateExpression="SET reply_state = :sent, reply_message_id = :mid",
            ExpressionAttributeValues={":sent": "sent", ":mid": str(note["id"])},
        )
        return "edited_replied"
    return "unknown_action"


def handler(event, context):
    token = params.get(DISCORD_TOKEN_PARAM, decrypt=True)
    channels = json.loads(params.get(DISCORD_CHANNELS_PARAM) or "{}")
    roles = json.loads(params.get(DISCORD_ROLES_PARAM) or "{}").get("roles") or {}
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
