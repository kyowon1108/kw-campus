"""Lambda: post or edit Discord messages for notice jobs from SQS.

The bot token comes from SSM (SecureString) and channel ids from /kw/discord/channels.
A delivery record (kw-delivery) is claimed before posting so an SQS retry never posts
the same notice twice.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import UTC, datetime

import boto3
from botocore.exceptions import ClientError

from kwcampus import params
from kwcampus.fetch import TLS
from kwcampus.config import (
    DELIVERY_TABLE,
    DELIVERY_TTL,
    DISCORD_CHANNELS_PARAM,
    DISCORD_TOKEN_PARAM,
    PUBLIC_TABLE,
)
from kwcampus.obs import log, metrics
from kwcampus.routing import CATEGORY_LABEL

API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/kyowon1108/kw-notice-mcp, 0.1)"
SUPPRESS_EMBEDS = 1 << 2
MAX_LEN = 2000

_MARKDOWN = re.compile(r"([\\*_~`|>\[\]()#])")


class RetryLater(Exception):
    """Discord asked us to slow down or failed transiently; let SQS redeliver."""


def escape(text: str) -> str:
    return _MARKDOWN.sub(r"\\\1", text)


def render(item: dict) -> str:
    label = CATEGORY_LABEL.get(str(item.get("category_id")), "공지")
    lines = [f"**[{label}]** {escape(str(item['title']))}"]
    meta = " · ".join(x for x in [str(item.get("department") or ""), str(item.get("posted_date") or "")] if x)
    if meta:
        lines.append(escape(meta))
    lines.append(f"<{item['url']}>")
    return "\n".join(lines)[:MAX_LEN]


def _discord(method: str, path: str, token: str, body: dict) -> dict:
    request = urllib.request.Request(  # noqa: S310 - fixed https host
        f"{API}{path}",
        data=json.dumps(body).encode(),
        method=method,
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=10, context=TLS) as response:  # noqa: S310
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code == 429 or e.code >= 500:
            raise RetryLater(f"discord {e.code}") from None
        raise RuntimeError(f"discord {e.code} on {method}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise RetryLater("discord unreachable") from e


def handle_job(job: dict, public, delivery, token: str, channels: dict[str, str], send=_discord) -> str:
    # A department channel is optional: until its id is set, department notices go to "notice".
    channel_id = channels.get(job["channel"]) or (channels.get("notice") if job["channel"] == "department" else None)
    if not channel_id:
        return "no_channel"
    item = public.get_item(Key=job["key"]).get("Item")
    if not item:
        return "gone"
    dkey = {"PK": f"{job['key']['PK']}|{job['key']['SK']}", "SK": f"CH#{job['channel']}"}
    content = render(item)

    if job["action"] == "post":
        now = datetime.now(UTC)
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
        message = send("POST", f"/channels/{channel_id}/messages", token,
                       {"content": content, "allowed_mentions": {"parse": []}, "flags": SUPPRESS_EMBEDS})
        delivery.update_item(
            Key=dkey,
            UpdateExpression="SET #s = :sent, message_id = :mid",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":sent": "sent", ":mid": str(message["id"])},
        )
        return "posted"

    if job["action"] == "edit":
        record = delivery.get_item(Key=dkey).get("Item")
        if not record or not record.get("message_id"):
            return "nothing_to_edit"
        send("PATCH", f"/channels/{record['channel_id']}/messages/{record['message_id']}", token,
             {"content": content, "allowed_mentions": {"parse": []}})
        return "edited"
    return "unknown_action"


def handler(event, context):  # noqa: ARG001
    token = params.get(DISCORD_TOKEN_PARAM, decrypt=True)
    channels = json.loads(params.get(DISCORD_CHANNELS_PARAM) or "{}")
    dynamodb = boto3.resource("dynamodb")
    public, delivery = dynamodb.Table(PUBLIC_TABLE), dynamodb.Table(DELIVERY_TABLE)

    failures, outcomes = [], {}
    # Discord tokens are plain ASCII; anything else means the wrong text was pasted into SSM.
    token_problem = None if token and token.isascii() and " " not in token.strip() else (
        "bot token not set" if not token else "bot token is not a Discord token (non-ASCII or spaces)")
    for record in event.get("Records", []):
        try:
            if token_problem:
                raise RetryLater(token_problem)
            outcome = handle_job(json.loads(record["body"]), public, delivery, token, channels)
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        except RetryLater as e:
            log("discord_retry", reason=str(e))
            failures.append({"itemIdentifier": record["messageId"]})
        except Exception as e:  # noqa: BLE001 - one bad job must not fail the batch; DLQ after retries
            log("discord_error", error=type(e).__name__, detail=str(e)[:120])
            failures.append({"itemIdentifier": record["messageId"]})
    log("discord_done", **outcomes, retries=len(failures))
    metrics(DiscordPosted=outcomes.get("posted", 0), DiscordRetries=len(failures))
    return {"batchItemFailures": failures}
